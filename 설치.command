#!/bin/sh
# macOS Finder entry point. Keep the existing installer as the single source.
CZZ_ROOT=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd) || exit 1
/bin/sh "$CZZ_ROOT/install.sh" "$@"
CZZ_RESULT=$?
if [ "$CZZ_RESULT" -eq 0 ]; then
  printf '\n%s\n' '설치가 완료되었습니다. 이 창을 닫고 실행.command를 더블클릭하세요.'
else
  printf '\n%s\n' '설치를 완료하지 못했습니다. 위 오류 내용을 확인해 주세요.'
fi
if [ -t 0 ]; then printf '%s' 'Enter를 누르면 종료합니다. '; read -r CZZ_REPLY; fi
exit "$CZZ_RESULT"
