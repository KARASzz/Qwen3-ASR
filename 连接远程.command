#!/bin/zsh
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/控制台.command" check
result=$?
printf '\n按回车关闭...'
IFS= read -r answer
exit "$result"
