#!/usr/bin/env python3
"""Submit local audio/video files to the configured remote Qwen3-ASR host."""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "remote-config.json"
INPUT_DIR = PROJECT_DIR / "输入"
OUTPUT_DIR = PROJECT_DIR / "输出"
KNOWN_HOSTS_PATH = PROJECT_DIR / ".remote_known_hosts"

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg",
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma",
}
CONNECT_TIMEOUT_SECONDS = 12
CONTROL_PERSIST_SECONDS = 900


class ClientError(Exception):
    """A safe, user-facing client or remote-operation error."""


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    port: int
    user: str
    remote_root: str
    identity_file: Optional[Path]
    expected_device_id: str

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


def _clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClientError(f"remote-config.json 的 {field} 必须是非空字符串。")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ClientError(f"remote-config.json 的 {field} 不能包含控制字符。")
    return value.strip()


def load_config() -> RemoteConfig:
    if CONFIG_PATH.is_symlink():
        raise ClientError("remote-config.json 不能是符号链接。")
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClientError("缺少 remote-config.json，尚未配置远程 SSH 主机。") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClientError(f"无法读取 remote-config.json：{exc}") from exc
    if not isinstance(raw, dict):
        raise ClientError("remote-config.json 顶层必须是 JSON 对象。")

    host = _clean_text(raw.get("host"), "host")
    user = _clean_text(raw.get("user"), "user")
    remote_root = _clean_text(raw.get("remote_root"), "remote_root")
    expected_device_id = _clean_text(raw.get("expected_device_id"), "expected_device_id").lower()
    if not re.fullmatch(r"[a-z0-9-]+", expected_device_id):
        raise ClientError("expected_device_id 必须是已核实的远端设备标识。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
        raise ClientError("host 只支持主机名或 IPv4 地址，且不能包含命令选项或空格。")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
        raise ClientError("user 格式无效；请使用 SSH 登录用户名。")
    try:
        port = int(raw.get("port"))
    except (TypeError, ValueError) as exc:
        raise ClientError("remote-config.json 的 port 必须是 1 到 65535 的整数。") from exc
    if not 1 <= port <= 65535:
        raise ClientError("remote-config.json 的 port 必须是 1 到 65535 的整数。")
    if not remote_root.startswith("/"):
        raise ClientError("remote_root 必须是远端绝对路径。")
    if any(part == ".." for part in remote_root.split("/")):
        raise ClientError("remote_root 不能包含 .. 路径段。")

    identity_file: Optional[Path] = None
    if raw.get("identity_file") not in (None, ""):
        identity_text = _clean_text(raw.get("identity_file"), "identity_file")
        candidate = Path(identity_text).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_DIR / candidate
        identity_file = candidate.resolve()
        if not identity_file.is_file():
            raise ClientError(
                "SSH 专用密钥不存在；请重新配置 SSH 专用密钥。"
            )
        if not os.access(identity_file, os.R_OK):
            raise ClientError("SSH 专用密钥不可读取；请检查其文件权限。")

    return RemoteConfig(host, port, user, remote_root.rstrip("/") or "/", identity_file, expected_device_id)


def _local_device_id() -> str:
    if sys.platform == "darwin":
        ioreg = shutil.which("ioreg") or "/usr/sbin/ioreg"
        try:
            result = subprocess.run(
                [ioreg, "-rd1", "-c", "IOPlatformExpertDevice"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClientError("无法读取本机设备 UUID，已拒绝提交任务。") from exc
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
        if result.returncode == 0 and match:
            return match.group(1).strip().lower()
    elif sys.platform.startswith("linux"):
        for id_path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = id_path.read_text(encoding="ascii").strip().lower()
            except (OSError, UnicodeError):
                continue
            if value:
                return value
    raise ClientError("无法确认本机设备身份，已拒绝提交任务。")


def _ensure_known_hosts_file() -> None:
    if KNOWN_HOSTS_PATH.is_symlink():
        raise ClientError(".remote_known_hosts 不能是符号链接。")
    try:
        fd = os.open(str(KNOWN_HOSTS_PATH), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(KNOWN_HOSTS_PATH, 0o600)
    except OSError as exc:
        raise ClientError(f"无法准备项目 SSH 主机密钥文件：{exc}") from exc


def _ssh_common_options(
    config: RemoteConfig, socket_path: Path, batch_mode: bool
) -> List[str]:
    options = [
        "-o", f'UserKnownHostsFile="{KNOWN_HOSTS_PATH}"',
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", f"ControlPath={socket_path}",
    ]
    if config.identity_file is not None:
        options.extend(["-i", str(config.identity_file)])
    if batch_mode:
        options.extend(["-o", "BatchMode=yes"])
    return options


class SSHSession:
    """One password prompt and one short-lived SSH ControlMaster per command."""

    def __init__(self, config: RemoteConfig, batch_mode: bool) -> None:
        self.config = config
        self.batch_mode = batch_mode
        self.socket_path = Path("/tmp") / (
            f"asr-{os.getuid()}-{uuid.uuid4().hex[:12]}"
        )
        self.started = False

    def __enter__(self) -> "SSHSession":
        _ensure_known_hosts_file()
        options = _ssh_common_options(self.config, self.socket_path, self.batch_mode)
        args = [
            "ssh", "-M", "-N", "-f", "-S", str(self.socket_path),
            "-p", str(self.config.port),
            "-o", "ControlMaster=yes",
            "-o", f"ControlPersist={CONTROL_PERSIST_SECONDS}",
            *options,
            self.config.target,
        ]
        try:
            result = subprocess.run(args, check=False, timeout=90)
            if result.returncode != 0:
                raise ClientError(
                    "SSH 登录失败。请检查远端连接、专用密钥及账号；首次配置请运行“连接远程.command”。"
                )
            self.started = True
            check = subprocess.run(
                [
                    "ssh", "-S", str(self.socket_path), "-p", str(self.config.port),
                    "-o", f'UserKnownHostsFile="{KNOWN_HOSTS_PATH}"',
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
                    "-O", "check", self.config.target,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if check.returncode != 0:
                raise ClientError("SSH 控制连接未能保持，已停止本次任务。")
            return self
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.close()
            raise ClientError(
                "无法建立 SSH 连接。请检查网络、远端 SSH 服务和专用密钥；需要时运行“连接远程.command”。"
            ) from exc
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.started:
            try:
                subprocess.run(
                    [
                        "ssh", "-S", str(self.socket_path), "-p", str(self.config.port),
                        "-o", f'UserKnownHostsFile="{KNOWN_HOSTS_PATH}"',
                        "-O", "exit", self.config.target,
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.started = False
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _ssh_args(self) -> List[str]:
        # A live ControlMaster has already authenticated. Slave commands must
        # never prompt again or consume input intended for the console menu.
        options = _ssh_common_options(self.config, self.socket_path, batch_mode=False)
        options.extend(["-o", "ControlMaster=auto", "-o", "BatchMode=yes"])
        return [
            "ssh", "-n", "-S", str(self.socket_path), "-p", str(self.config.port),
            *options,
            self.config.target,
        ]

    def run_remote(
        self, script: str, *, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        command = "sh -c " + shlex.quote(script)
        try:
            if capture:
                return subprocess.run(
                    [*self._ssh_args(), command],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            return subprocess.run([*self._ssh_args(), command], check=False)
        except OSError as exc:
            raise ClientError(f"无法启动系统 SSH 客户端：{exc}") from exc

    def scp(self, source: str, destination: str, *, recursive: bool = False) -> int:
        args = ["scp"]
        if recursive:
            args.append("-r")
        args.extend(["-P", str(self.config.port)])
        args.extend(
            [
                "-o", f'UserKnownHostsFile="{KNOWN_HOSTS_PATH}"',
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "IdentitiesOnly=yes",
                "-o", f"ControlPath={self.socket_path}",
                "-o", "ControlMaster=auto",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
            ]
        )
        if self.config.identity_file is not None:
            args.extend(["-i", str(self.config.identity_file)])
        args.extend([source, destination])
        try:
            result = subprocess.run(args, check=False)
        except OSError as exc:
            raise ClientError(f"无法启动系统 scp 客户端：{exc}") from exc
        return result.returncode


def _remote_preflight_script(remote_root: str) -> str:
    root_literal = shlex.quote(remote_root)
    return f"""set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
root={root_literal}
if [ ! -d "$root" ]; then
  printf '%s\\n' 'ASR_ERROR=remote_root_missing'
  exit 41
fi
root_real=$(CDPATH= cd "$root" 2>/dev/null && pwd -P) || {{
  printf '%s\\n' 'ASR_ERROR=remote_root_unreadable'
  exit 42
}}
runner="$root_real/转写.command"
if [ ! -x "$runner" ]; then
  printf '%s\\n' 'ASR_ERROR=runner_missing_or_not_executable'
  exit 43
fi
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\\n' 'ASR_ERROR=remote_uv_missing'
  exit 45
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  printf '%s\\n' 'ASR_ERROR=remote_ffmpeg_missing'
  exit 46
fi
model_dir="$root_real/models/Qwen3-ASR-1.7B"
if [ ! -s "$model_dir/config.json" ]; then
  printf '%s\\n' 'ASR_ERROR=remote_model_config_missing'
  exit 47
fi
if [ ! -s "$model_dir/model.safetensors.index.json" ]; then
  printf '%s\\n' 'ASR_ERROR=remote_model_index_missing'
  exit 48
fi
if [ ! -s "$model_dir/model-00001-of-00002.safetensors" ] || \\
   [ ! -s "$model_dir/model-00002-of-00002.safetensors" ]; then
  printf '%s\\n' 'ASR_ERROR=remote_model_weights_missing'
  exit 49
fi
host=$(hostname 2>/dev/null || uname -n 2>/dev/null || printf unknown)
user=$(id -un 2>/dev/null || whoami 2>/dev/null || printf unknown)
system=$(uname -s 2>/dev/null || printf unknown)
memory=unknown
if [ "$system" = Darwin ]; then
  bytes=$(sysctl -n hw.memsize 2>/dev/null || printf 0)
  case "$bytes" in
    ''|*[!0-9]*) memory=unknown ;;
    *) memory=$(awk -v n="$bytes" 'BEGIN {{ printf "%.1f GiB", n / 1073741824 }}') ;;
  esac
elif [ "$system" = Linux ] && [ -r /proc/meminfo ]; then
  memory=$(awk '/^MemTotal:/ {{ printf "%.1f GiB", $2 / 1048576 }}' /proc/meminfo)
fi
device_id=
case "$system" in
  Darwin)
    device_id=$(/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | \
      /usr/bin/sed -n 's/.*"IOPlatformUUID" = "\\([^"]*\\)".*/\\1/p' | \
      /usr/bin/head -n 1)
    ;;
  Linux)
    device_id=$(cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null || true)
    ;;
esac
if [ -z "$device_id" ]; then
  printf '%s\\n' 'ASR_ERROR=remote_device_identity_unavailable'
  exit 44
fi
printf 'ASR_HOST=%s\\n' "$host"
printf 'ASR_USER=%s\\n' "$user"
printf 'ASR_SYSTEM=%s\\n' "$system"
printf 'ASR_MEMORY=%s\\n' "$memory"
printf 'ASR_ROOT=%s\\n' "$root_real"
printf 'ASR_DEVICE_ID=%s\\n' "$device_id"
"""


def _parse_preflight(result: subprocess.CompletedProcess[str], config: RemoteConfig) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if line.startswith("ASR_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if result.returncode != 0:
        reason = values.get("ASR_ERROR", "remote_preflight_failed")
        messages = {
            "remote_root_missing": "远端配置目录不存在。请核对 remote_root。",
            "remote_root_unreadable": "远端配置目录无法读取。",
            "runner_missing_or_not_executable": "远端“转写.command”不存在或不可执行。",
            "remote_device_identity_unavailable": "无法确认远端设备身份，已拒绝运行。",
            "remote_uv_missing": "远端未找到 uv；检查 PATH 与远端运行环境。",
            "remote_ffmpeg_missing": "远端未找到 ffmpeg；检查 PATH 与远端运行环境。",
            "remote_model_config_missing": "远端模型目录缺少非空 config.json。",
            "remote_model_index_missing": "远端模型目录缺少非空模型权重索引。",
            "remote_model_weights_missing": "远端模型目录缺少一个或多个非空 safetensors 权重分片。",
        }
        detail = messages.get(reason, "远端预检失败。")
        if result.stderr and result.stderr.strip():
            detail += " SSH 返回：" + result.stderr.strip().splitlines()[-1]
        raise ClientError(detail)
    required = ("ASR_HOST", "ASR_USER", "ASR_SYSTEM", "ASR_MEMORY", "ASR_ROOT", "ASR_DEVICE_ID")
    if any(not values.get(key) for key in required):
        raise ClientError("远端预检没有返回完整设备信息，已拒绝运行。")
    if values["ASR_USER"] != config.user:
        raise ClientError("SSH 登录后的远端用户名与配置不一致，已拒绝运行。")
    local_id = _local_device_id()
    if values["ASR_DEVICE_ID"].strip().lower() == local_id:
        raise ClientError("远端设备身份与本机相同，已拒绝在本机运行模型。")
    if values["ASR_DEVICE_ID"].strip().lower() != config.expected_device_id:
        raise ClientError("远端设备标识与已配置的计算设备不符，已拒绝上传和转写。")
    return values


def _print_remote_info(info: Dict[str, str]) -> None:
    print("远端预检通过：")
    print(f"  主机：{info['ASR_HOST']}")
    print(f"  用户：{info['ASR_USER']}")
    print(f"  系统：{info['ASR_SYSTEM']}")
    print(f"  内存：{info['ASR_MEMORY']}")
    print(f"  真实目录：{info['ASR_ROOT']}")
    print("  已确认远端设备与本机不同；未在本机加载模型。")


def _create_batch_dir() -> Tuple[str, Path]:
    if INPUT_DIR.is_symlink():
        raise ClientError("“输入”不能是符号链接；请将媒体文件放入项目的输入文件夹。")
    if not INPUT_DIR.is_dir():
        raise ClientError(f"输入文件夹不存在：{INPUT_DIR}")
    if OUTPUT_DIR.is_symlink():
        raise ClientError("“输出”不能是符号链接；为保护其他目录，本次任务已停止。")
    try:
        OUTPUT_DIR.mkdir(exist_ok=True)
    except OSError as exc:
        raise ClientError(f"无法创建输出文件夹：{exc}") from exc
    project_real = PROJECT_DIR.resolve()
    if OUTPUT_DIR.resolve().parent != project_real:
        raise ClientError("输出文件夹不在本项目内，已拒绝写入。")
    batch_id = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12]
    batch_dir = OUTPUT_DIR / batch_id
    try:
        batch_dir.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise ClientError(f"无法创建本次任务的唯一输出目录：{exc}") from exc
    return batch_id, batch_dir


def _collect_inputs() -> List[Path]:
    if INPUT_DIR.is_symlink() or not INPUT_DIR.is_dir():
        raise ClientError(f"输入文件夹不存在或不是项目内普通目录：{INPUT_DIR}")
    try:
        candidates = [
            path for path in INPUT_DIR.iterdir()
            if not path.is_symlink() and path.is_file()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    except OSError as exc:
        raise ClientError(f"无法读取输入文件夹：{exc}") from exc
    return sorted(candidates, key=lambda path: (path.name.casefold(), path.name))


def _safe_stem(stem: str) -> str:
    visible = "".join("_" if ord(ch) < 32 or ord(ch) == 127 else ch for ch in stem)
    encoded = visible.encode("utf-8", errors="ignore")[:180]
    result = encoded.decode("utf-8", errors="ignore").rstrip(" .")
    return result or "audio"


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    temp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=True, indent=2) + "\n"
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ClientError(f"无法保存本次任务状态：{exc}") from exc


def _valid_result(path: Path, fmt: str) -> bool:
    try:
        with path.open("r", encoding="utf-8") as stream:
            if fmt == "txt":
                return any(line.strip() for line in stream)
            awaiting_text = False
            for line in stream:
                line = line.strip()
                if not line:
                    awaiting_text = False
                elif re.fullmatch(r"\d{2,}:\d{2}:\d{2},\d{3} --> \d{2,}:\d{2}:\d{2},\d{3}", line):
                    awaiting_text = True
                elif awaiting_text:
                    return True
    except (OSError, UnicodeError):
        return False
    return False


def _result_candidates(local_dir: Path, fmt: str) -> List[Path]:
    suffix = "." + fmt.lower()
    found: List[Path] = []
    if not local_dir.is_dir() or local_dir.is_symlink():
        return found
    for path in local_dir.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != suffix:
                continue
            if path.stat().st_size > 0 and _valid_result(path, fmt):
                found.append(path)
        except OSError:
            continue
    return sorted(found, key=lambda path: str(path.relative_to(local_dir)))


def _publish_result(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ClientError(f"结果文件已存在，拒绝覆盖：{target.name}")
    temp = target.with_name("." + target.name + "." + uuid.uuid4().hex[:8] + ".part")
    try:
        with source.open("rb") as src, temp.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if temp.stat().st_size <= 0:
            raise ClientError("下载的结果文件为空。")
        os.link(temp, target)
        temp.unlink()
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = _datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    _atomic_write_json(path, state)


def _remote_job_setup_script(remote_job_dir: str) -> str:
    job = shlex.quote(remote_job_dir)
    return f"""set -eu
umask 077
if [ -e {job} ]; then
  printf '%s\\n' 'ASR_ERROR=job_directory_already_exists'
  exit 51
fi
mkdir -p -m 700 {shlex.quote(remote_job_dir + '/input')} {shlex.quote(remote_job_dir + '/output')}
"""


def _runner_script(runner: str, remote_input: str, remote_output: str, fmt: str) -> str:
    # PATH changes are inside the remote shell only. This client never launches a local model.
    return "\n".join(
        [
            "set -eu",
            'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"',
            f"mkdir -p -m 700 {shlex.quote(remote_output)}",
            f"{shlex.quote(runner)} {shlex.quote(remote_input)} -f {fmt} -o {shlex.quote(remote_output)} --quiet",
        ]
    ) + "\n"


def _cleanup_remote_job(session: SSHSession, remote_job_dir: str) -> bool:
    if not re.fullmatch(r"/tmp/qwen3-asr-[0-9a-f]{32}", remote_job_dir):
        raise ClientError("远端临时目录格式异常，已拒绝清理。")
    job = shlex.quote(remote_job_dir)
    result = session.run_remote(f"set -eu\nrm -rf -- {job}\ntest ! -e {job}\n", capture=True)
    return result.returncode == 0


def run_check(batch_mode: bool = False) -> int:
    config = load_config()
    try:
        with SSHSession(config, batch_mode) as session:
            result = session.run_remote(_remote_preflight_script(config.remote_root), capture=True)
            info = _parse_preflight(result, config)
            _print_remote_info(info)
            print("检查仅验证 SSH、远端目录、入口和设备身份；没有启动转写模型。")
        return 0
    except ClientError:
        raise


def run_batch(fmt: str, batch_mode: bool = False) -> int:
    config = load_config()
    inputs = _collect_inputs()
    if not inputs:
        print(f"“输入”目录里没有支持的音视频文件：{INPUT_DIR}")
        return 1

    batch_id, batch_dir = _create_batch_dir()
    state_path = batch_dir / "job.json"
    remote_job_id = "qwen3-asr-" + uuid.uuid4().hex
    remote_job_dir = "/tmp/" + remote_job_id
    state: Dict[str, Any] = {
        "job_id": batch_id,
        "remote_job_id": remote_job_id,
        "remote_job_dir": remote_job_dir,
        "remote_job_created": False,
        "created_at": _datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated_at": None,
        "format": fmt,
        "status": "preparing",
        "remote": {
            "host": config.host,
            "port": config.port,
            "user": config.user,
            "configured_root": config.remote_root,
        },
        "local_output_dir": str(batch_dir),
        "inputs": [
            {
                "sequence": f"{index:05d}",
                "source_name": path.name,
                "upload_name": f"{index:05d}{path.suffix.lower()}",
                "status": "pending",
                "output_name": None,
                "error": None,
            }
            for index, path in enumerate(inputs, start=1)
        ],
        "errors": [],
        "remote_cleanup": "retained for retry and review",
    }
    _save_state(state_path, state)
    print(f"本次任务：{batch_id}")
    print(f"本地状态与结果：{batch_dir}")

    session: Optional[SSHSession] = None
    try:
        with SSHSession(config, batch_mode) as active_session:
            session = active_session
            preflight = active_session.run_remote(
                _remote_preflight_script(config.remote_root), capture=True
            )
            info = _parse_preflight(preflight, config)
            state["remote"].update(
                {
                    "hostname": info["ASR_HOST"],
                    "username": info["ASR_USER"],
                    "system": info["ASR_SYSTEM"],
                    "memory": info["ASR_MEMORY"],
                    "real_root": info["ASR_ROOT"],
                }
            )
            state["status"] = "preflight_passed"
            _save_state(state_path, state)
            _print_remote_info(info)

            setup = active_session.run_remote(_remote_job_setup_script(remote_job_dir), capture=True)
            if setup.returncode != 0:
                detail = (setup.stderr or setup.stdout or "").strip().splitlines()
                raise ClientError("无法在远端建立本次作业目录。" + (" " + detail[-1] if detail else ""))
            state["remote_job_created"] = True
            state["status"] = "uploading"
            _save_state(state_path, state)

            for index, source_path in enumerate(inputs, start=1):
                item = state["inputs"][index - 1]
                remote_input = f"{remote_job_dir}/input/{item['upload_name']}"
                item["status"] = "uploading"
                _save_state(state_path, state)
                remote_spec = f"{config.target}:{remote_input}"
                if active_session.scp(str(source_path.resolve()), remote_spec) != 0:
                    item["status"] = "upload_failed"
                    item["error"] = "scp 上传失败；未在本机运行转写。"
                    state["status"] = "failed"
                    state["errors"].append(f"上传失败：{source_path.name}")
                    _save_state(state_path, state)
                    print(f"上传失败：{source_path.name}。远端作业保留在 {remote_job_dir}", file=sys.stderr)
                    return 1
                item["status"] = "uploaded"
                _save_state(state_path, state)

            staging_root = batch_dir / ".staging"
            staging_root.mkdir(mode=0o700, exist_ok=True)
            failures = 0
            successes = 0
            runner = info["ASR_ROOT"].rstrip("/") + "/转写.command"

            for index, source_path in enumerate(inputs, start=1):
                item = state["inputs"][index - 1]
                sequence = item["sequence"]
                remote_input = f"{remote_job_dir}/input/{item['upload_name']}"
                remote_output = f"{remote_job_dir}/output/{sequence}"
                local_result_dir = staging_root / sequence
                item["status"] = "running"
                state["status"] = "running"
                _save_state(state_path, state)
                print(f"\n远端转写：{source_path.name}")
                run_result = active_session.run_remote(
                    _runner_script(runner, remote_input, remote_output, fmt), capture=False
                )
                item["remote_returncode"] = run_result.returncode
                if run_result.returncode != 0:
                    item["status"] = "remote_failed"
                    item["error"] = f"远端转写返回状态码 {run_result.returncode}；未在本机回退。"
                    state["errors"].append(f"远端转写失败：{source_path.name}")
                    failures += 1
                    _save_state(state_path, state)
                    print(f"远端转写失败：{source_path.name}；远端作业保留。", file=sys.stderr)
                    continue

                item["status"] = "downloading"
                _save_state(state_path, state)
                local_result_dir.parent.mkdir(mode=0o700, exist_ok=True)
                remote_output_spec = f"{config.target}:{remote_output}"
                if active_session.scp(remote_output_spec, str(staging_root), recursive=True) != 0:
                    item["status"] = "download_failed"
                    item["error"] = f"结果下载失败；可按状态文件中的远端路径重新取回。作业 ID：{remote_job_id}。"
                    state["errors"].append(f"下载失败：{source_path.name}")
                    failures += 1
                    _save_state(state_path, state)
                    print(
                        f"结果下载失败：{source_path.name}。远端结果保留在 {remote_job_dir}，"
                        f"本地状态文件：{state_path}",
                        file=sys.stderr,
                    )
                    continue

                # The CLI names output from the uploaded input stem, but we still inspect
                # the complete per-file directory and require one non-empty matching result.
                candidates = _result_candidates(local_result_dir, fmt)
                if len(candidates) != 1:
                    item["status"] = "output_ambiguous" if candidates else "output_missing"
                    item["error"] = (
                        f"远端输出目录中发现 {len(candidates)} 个有效 .{fmt} 结果；"
                        "需要恰好一个才能安全命名。"
                    )
                    state["errors"].append(f"结果校验失败：{source_path.name}")
                    failures += 1
                    _save_state(state_path, state)
                    print(f"结果校验失败：{source_path.name}；远端作业保留。", file=sys.stderr)
                    continue

                output_name = f"{index:03d}-{_safe_stem(source_path.stem)}.{fmt}"
                output_path = batch_dir / output_name
                try:
                    _publish_result(candidates[0], output_path)
                except (OSError, ClientError) as exc:
                    item["status"] = "local_save_failed"
                    item["error"] = str(exc)
                    state["errors"].append(f"本地保存失败：{source_path.name}")
                    failures += 1
                    _save_state(state_path, state)
                    print(f"本地结果保存失败：{source_path.name}；远端作业保留。", file=sys.stderr)
                    continue

                item["status"] = "completed"
                item["output_name"] = output_name
                item["output_size_bytes"] = output_path.stat().st_size
                successes += 1
                _save_state(state_path, state)
                print(f"已保存：{output_path}")

            if successes == len(inputs) and failures == 0:
                state["status"] = "completed"
                try:
                    cleanup_ok = _cleanup_remote_job(active_session, remote_job_dir)
                except ClientError:
                    cleanup_ok = False
                if cleanup_ok:
                    state["remote_cleanup"] = "removed_after_verified_download"
                else:
                    state["remote_cleanup"] = "cleanup_failed_retained"
                    print(f"结果已取回，但远端临时副本清理失败，保留在：{remote_job_dir}", file=sys.stderr)
                try:
                    shutil.rmtree(staging_root)
                except OSError:
                    pass
                _save_state(state_path, state)
                print(f"\n全部 {successes} 个文件已远端转写并下载到：\n{batch_dir}")
                if state["remote_cleanup"] == "removed_after_verified_download":
                    print("已清理远端本次作业的临时副本，本机输入和结果保留。")
                return 0

            state["status"] = "partial" if successes else "failed"
            _save_state(state_path, state)
            print(
                f"\n批次未全部成功：成功 {successes}/{len(inputs)}，失败 {failures}。"
                f"\n状态文件：{state_path}\n远端作业保留：{remote_job_dir}",
                file=sys.stderr,
            )
            return 1
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        state["errors"].append("用户中断；本机未回退，远端作业保留。")
        try:
            _save_state(state_path, state)
        except ClientError:
            pass
        print(f"\n任务已中断；远端作业保留：{remote_job_dir}\n状态文件：{state_path}", file=sys.stderr)
        return 130
    except ClientError as exc:
        state["status"] = "failed"
        state["errors"].append(str(exc))
        try:
            _save_state(state_path, state)
        except ClientError as state_exc:
            print(f"无法写入失败状态：{state_exc}", file=sys.stderr)
        print(f"任务失败：{exc}\n状态文件：{state_path}", file=sys.stderr)
        if state.get("remote_job_created"):
            print(f"远端作业（如已创建）保留：{remote_job_dir}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过 SSH 将输入文件发送到远端 Qwen3-ASR，并取回结果。",
        epilog="不带子命令时只显示帮助；本客户端不会在本机加载模型。",
    )
    commands = parser.add_subparsers(dest="command")
    check_parser = commands.add_parser("check", help="只检查 SSH、远端目录和设备身份")
    check_parser.add_argument(
        "--batch-mode", action="store_true",
        help="禁用 SSH 交互提示，供非交互连接验证使用。",
    )
    batch_parser = commands.add_parser("batch", help="上传输入目录内的音视频并远端转写")
    batch_parser.add_argument("--format", required=True, choices=("txt", "srt"))
    batch_parser.add_argument(
        "--batch-mode", action="store_true",
        help="禁用 SSH 交互提示，供非交互连接验证使用。",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "check":
            return run_check(args.batch_mode)
        if args.command == "batch":
            return run_batch(args.format, args.batch_mode)
    except ClientError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
