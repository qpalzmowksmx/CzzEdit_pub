#!/bin/sh
# .command opens in Terminal when launched from Finder on macOS.
CZZ_ROOT=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd) || exit 1
printf '%s\n' 'CzzEdit를 실행합니다. 종료하려면 이 터미널에서 Ctrl+C를 누르세요.'
/bin/sh "$CZZ_ROOT/run.sh" "$@"
CZZ_RESULT=$?
if [ "$CZZ_RESULT" -ne 0 ] && [ "$CZZ_RESULT" -ne 130 ]; then
  printf '\n%s\n' '실행하지 못했습니다. 처음 사용하는 폴더라면 설치.command를 먼저 실행하세요.'
fi
if [ -t 0 ]; then printf '%s' 'Enter를 누르면 종료합니다. '; read -r CZZ_REPLY; fi
exit "$CZZ_RESULT"
