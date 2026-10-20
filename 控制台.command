#!/bin/zsh
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
INPUT_DIR="$ROOT/输入"
OUTPUT_DIR="$ROOT/输出"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"

run_remote_client() {
  if ! command -v python3 >/dev/null 2>&1; then
    printf '\n找不到轻量远程客户端所需的 Python 3。\n' >&2
    return 1
  fi
  python3 "$ROOT/remote_client.py" "$@"
}

pause_menu() {
  printf '\n按回车返回主菜单...'
  IFS= read -r pause_value
}

if (( $# > 0 )); then
  run_remote_client "$@"
  exit $?
fi

while true; do
  printf '\n========================================\n'
  printf '          Qwen3-ASR 远程转写\n'
  printf '========================================\n'
  printf '输入：%s\n输出：%s\n' "$INPUT_DIR" "$OUTPUT_DIR"
  printf '模型在远端 Mac 执行，本机只提交文件和接收结果。\n'
  printf '\n  1) 远程批量转写为 TXT 文稿\n'
  printf '  2) 远程批量生成 SRT 字幕\n'
  printf '  3) 打开输入文件夹\n'
  printf '  4) 打开输出文件夹\n'
  printf '  5) 检查远程连接与运行环境\n'
  printf '  0) 退出\n'
  printf '\n请选择：'
  if ! IFS= read -r choice; then
    exit 0
  fi
  case "$choice" in
    1) run_remote_client batch --format txt; pause_menu ;;
    2) run_remote_client batch --format srt; pause_menu ;;
    3) open "$INPUT_DIR" ;;
    4) open "$OUTPUT_DIR" ;;
    5) run_remote_client check; pause_menu ;;
    0) exit 0 ;;
    *) printf '无效选项，请重新输入。\n' ;;
  esac
done

