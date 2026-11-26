# 로컬 개발 및 검증

이 프로젝트는 로컬 프로토타입입니다. 현재 서버는 `127.0.0.1`에서 실행하며 LocalStack을 사용합니다. 운영 환경에 공개하거나 실제 회사 토큰·비공개 자료를 로컬 에뮬레이터에 저장하지 마세요. 아키텍처 및 미구현 보안 경계는 [아키텍처 문서](architecture.md)를 확인하세요.

## 준비와 실행

Docker, Python 3.11 이상, [uv](https://docs.astral.sh/uv/)가 필요합니다. 저장소 루트에서 실행합니다.

```sh
uv sync --no-install-project
docker compose up -d --wait
uv run --no-sync python init_local.py
uv run --no-sync python app.py
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다. 에뮬레이터는 `127.0.0.1:4568`에 바인딩하고 S3, DynamoDB, Secrets Manager, KMS를 제공합니다. 계정 연결 상태 테스트는 임시 DynamoDB 테이블과 KMS 키를 생성하고 정리합니다. `init_local.py`는 필요한 리소스를 생성하며 기존 데이터를 유지합니다. 이 구성에는 Neptune이나 OpenSearch 에뮬레이터가 없고 실제 AWS 리소스도 생성하지 않습니다.

개발이 끝나면 `docker compose down`으로 컨테이너를 종료합니다. 데이터 볼륨은 남습니다. `docker compose down -v`는 해당 볼륨의 데이터를 삭제하므로 필요할 때만 실행하세요.

## 테스트

```sh
# 로컬 에뮬레이터가 없어도 실행할 수 있는 단위 테스트
uv run --no-sync pytest -q

# 실행 중인 LocalStack이 필요합니다.
TEAM_CONTEXT_INTEGRATION=1 uv run --no-sync pytest -q tests/test_localstack.py

# 기존 mutants/ 결과를 지운 뒤 전체 백엔드 변이 검사
python3 -c "import shutil; shutil.rmtree('mutants', ignore_errors=True)"
uv run --no-sync mutmut run --max-children 4
uv run --no-sync mutmut run --max-children 2 'auth.coa_identity.*'
uv run --no-sync mutmut run --max-children 2 'auth.link_attempts.*'
uv run --no-sync mutmut run --max-children 2 'auth.link_completion.*'
uv run --no-sync mutmut run --max-children 2 'auth.source_credentials.*'
uv run --no-sync mutmut run --max-children 2 'auth.source_provenance.*'
uv run --no-sync mutmut export-cicd-stats
uv run --no-sync python scripts/check_mutation.py mutants/mutmut-cicd-stats.json
```

통합 테스트는 생성한 테스트 데이터를 삭제합니다. 각 테스트가 만든 자원만 정리하도록 유지하세요. 변이 게이트는 백엔드 Python 코드의 탐지한 변이 수를 전체 변이 수로 나누며 90% 이상을 요구합니다. 도구 오류나 테스트되지 않은 변이는 탐지로 계산하지 않습니다. 현재 UI JavaScript에는 별도 변이 게이트가 없으며 [#59](https://github.com/YoungJinJung/team-context/issues/59)에서 추적합니다. 외부 서비스의 실제 OAuth 연결은 가짜 응답을 사용한 테스트만으로 검증되지 않습니다.

## 인증 개발

공급자 OAuth 앱 등록과 환경 변수는 [README의 OAuth 연결 안내](../README.md#connect-an-oauth-app)을 참고하세요. 개인 API 토큰을 입력하는 흐름은 제공하지 않습니다. GitHub App의 앱 비밀처럼 공급자가 요구하는 앱 자격증명은 서버에서 다루며 브라우저나 로그로 내보내지 않습니다. 현재 비밀 저장은 LocalStack이므로 실제 자격증명을 넣지 말고 테스트용 가짜 값과 모의 공급자 응답을 사용하세요.

COA 인증과 계정 연결 모듈은 HTTP 엔드포인트나 MCP에 연결되지 않은 내부 구성요소입니다. 연결 완료 시 사용자와 공급자별 비밀을 KMS로 암호화하는 Secrets Manager에 저장합니다. 연결 해제는 저장된 비밀만 삭제하며 공급자의 권한 취소나 수집 데이터 삭제를 수행하지 않습니다. 운영 연동에는 사용자별 원본 권한 재검증과 데이터 삭제 전파가 필요합니다.

내부 자격 증명 조회는 매 요청에서 COA 신원을 검증하고 사용자와 공급자가 일치하는 만료 전 토큰만 반환합니다. 이 값은 내부 코드 전용이며 로그나 HTTP·MCP 응답에 포함하면 안 됩니다. 토큰 갱신과 COA 조회 경로의 원본 접근 권한 강제는 아직 구현하지 않았습니다.

GitHub.com PR과 GitLab.com MR의 내부 출처 검증은 조회자의 연결된 토큰으로 저장소 ID와 원본 항목을 다시 확인하고 수집 시 기록한 정규화된 내용의 해시가 현재 내용과 일치할 때만 허용합니다. 정규화된 내용은 현재 제목, 본문, URL, 수정 시각과 항목·저장소 ID로 제한되며 코드 변경 내용과 댓글은 포함하지 않습니다. 출처 목록은 신뢰할 수 있는 완전한 인덱스에서 제공해야 합니다. 아직 이 인덱스, COA 조회 전 필터링, 다른 공급자와 직접 접근 경로 차단이 없으므로 이 구성요소만으로 비공개 COA 조회를 활성화할 수 없습니다.

로컬 GitLab 비공개 스냅샷 목록·삭제 명령도 README에 있습니다. 이 명령은 로컬 운영자용이며, 운영 환경의 관리자 권한 또는 보존 정책을 대신하지 않습니다.

## 변경 범위

- 공개 커넥터 계약과 비공개 접근 시 고려할 점은 [커넥터 안내](connectors.md)를 확인합니다.
- 원본 응답, 스토리지 또는 비밀 저장소의 오류가 발생할 때 민감한 데이터가 노출되지 않는지 테스트합니다.
- 사용자별 권한을 가진 외부 원본을 모의하는 경우 사용자 A의 수집 권한과 사용자 B의 조회 권한을 별도로 테스트합니다.
- PR에는 실제 서비스 통합 여부, 로컬 에뮬레이터 테스트, 변이 점수와 알려진 한계를 함께 기록합니다.
