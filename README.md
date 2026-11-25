# Team Context

팀의 문서, 코드, 대화, 업무 티켓을 공통 형식으로 연결하기 위한 오픈소스 프로젝트입니다.

## 현재 상태

초기 프로토타입입니다. GitHub **공개 저장소의 최근 PR 최대 30개**를 실제로 가져와 화면에 표시합니다. 인증이나 데이터 저장은 아직 지원하지 않습니다. 로컬 개발용이며 인터넷에 공개해 운영하지 마세요.

## 실행

Python 3.11 이상이 필요합니다. 추가 패키지는 필요하지 않습니다.

```sh
python3 app.py
```

브라우저에서 http://127.0.0.1:8765 를 열고 `owner/repository`를 입력하세요. GitHub API의 인증 없는 요청 제한이 적용됩니다.

## 커넥터 만들기

`connectors/`에 Python 모듈을 추가하면 실행기가 자동으로 찾습니다. 모듈은 `MANIFEST`와 `collect(config)`를 제공합니다. `MANIFEST`의 `fields`는 연결 화면의 입력 항목으로 표시됩니다. `collect`는 `id`, `type`, `title`, `url`, `updated_at`, `body`를 가진 레코드 목록을 반환합니다. 예제는 [GitHub 커넥터](connectors/github.py)를 확인하세요. 서버 재시작 후 새 커넥터가 표시됩니다. 신뢰할 수 있는 커넥터 코드만 설치하세요.

```sh
python3 -m unittest discover -s tests -v
```

## 다음 단계

- GitHub App 인증과 비공개 저장소 접근, 페이지 처리 및 변경분 동기화
- Jira, GitLab, Linear, Confluence, Teams 커넥터
- 원본 권한·삭제 동기화, 안전한 인증 정보 저장
- 로컬 저장과 검색, AWS 배포 및 COA 연동

커넥터 계약과 기능은 초기 단계로 변경될 수 있습니다.
