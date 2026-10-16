# PostgreSQL High Availability & Disaster Recovery Lab

PostgreSQL 장애 상황에서 데이터 유실 범위와 쓰기 복구 시간을 직접 측정하는 DBA 포트폴리오 프로젝트입니다.

Primary와 Standby의 복제부터 시작해 장애 전환, 재시도 정합성, 기존 Primary 재편입, WAL 기반 PITR을 단계별로 구현할 계획입니다. 단일 PC의 Docker 환경에서 실험하며 운영 환경의 고가용성이나 무손실을 보장하는 프로젝트는 아닙니다.

## 현재 단계

Python 실행용 `runner`와 PostgreSQL Primary 한 대를 구성했습니다. runner에서 SQL로 접속하고, 트랜잭션을 커밋한 뒤 다른 연결에서 조회하는 것까지 확인합니다. Standby와 복제는 다음 단계에서 추가합니다.

| 구성 요소 | 버전 |
|---|---|
| PostgreSQL | 18.0-bookworm |
| runner Python | 3.12.11 |
| psycopg | 3.2.9 |
| pytest | 8.4.1 |

Python 패키지는 컨테이너 안에 설치합니다. 호스트에 Python이나 별도 패키지를 설치할 필요가 없습니다.

## 실행 환경 준비

Windows에서는 Docker Desktop을 설치하고 Linux 컨테이너 모드로 실행합니다. Docker Compose v2와 Git도 필요합니다.

PowerShell에서 저장소를 내려받은 뒤 실행합니다.

```powershell
git clone https://github.com/wlsqc123/postgres-ha-dr-lab.git
cd postgres-ha-dr-lab

docker version
docker compose version
docker compose config --quiet
docker compose up -d --build runner
```

빌드 시 Python 이미지와 패키지를 내려받으므로 인터넷 연결이 필요합니다. 저장소 이름을 그대로 쓰면 별도 `.env` 설정 없이 실행됩니다. 다른 Compose 프로젝트 이름이 필요하면 `.env.example`을 `.env`로 복사하고 값을 변경합니다.

## 준비 상태 확인

```powershell
docker compose ps
docker compose exec -T runner python --version
docker compose exec -T runner python -c "import psycopg, pytest; print('psycopg:', psycopg.__version__); print('pytest:', pytest.__version__)"
```

`runner`가 실행 중이고 Python `3.12.11`, psycopg `3.2.9`, pytest `8.4.1`이 출력되면 Python 환경 준비가 끝납니다.

## Primary 실행과 SQL 검증

```powershell
docker compose up -d --build --wait primary runner
docker compose exec -T runner python scripts/check_primary.py
```

Primary는 데이터베이스 `lab`, 사용자 `postgres`, 로컬 실습용 비밀번호 `local-admin`으로 초기화합니다. 비밀번호는 `.env`의 `POSTGRES_PASSWORD`로 변경할 수 있지만, 이미 초기화된 데이터베이스의 비밀번호가 자동으로 바뀌지는 않습니다. 운영 환경에서 이 공개된 비밀번호를 사용하지 않습니다.

호스트에 DB 포트를 열지 않습니다. runner는 내부 Docker 네트워크에서 `primary:5432`에 접속합니다. 명령줄로 직접 접속하려면 다음과 같이 실행합니다.

```powershell
docker compose exec primary psql -U postgres -d lab
```

`check_primary.py`는 PostgreSQL 18, 복구 중이 아닌 쓰기 가능한 상태, 데이터 체크섬을 확인합니다. `environment_probe` 테이블에 고정된 테스트 행을 커밋하고, 새 연결에서 기대값 `{id: 1, value: primary-ready}`와 비교합니다. `pg_is_in_recovery()`가 `false`, `read_only`가 `off`, `data_checksums`가 `on`이고 마지막 `success`가 `true`여야 합니다. 실패하면 0이 아닌 종료 코드를 반환합니다.

재시작 후에도 데이터가 남는지 확인합니다.

```powershell
docker compose restart primary
docker compose up -d --wait primary runner
docker compose exec -T runner python scripts/check_primary.py --verify-only
```

`--verify-only`는 데이터를 다시 넣지 않고 기존 행만 확인합니다. 이 검증은 단일 Primary의 데이터 보존 확인이며 장애 전환 실험은 아닙니다.

데이터는 `primary_stage1` 이름의 Compose 볼륨에 저장합니다. 기본 프로젝트 이름에서는 실제 볼륨 이름이 `postgres-ha-dr-lab_primary_stage1`입니다. 이전 실험 볼륨을 재사용하지 않습니다. `docker compose down -v`는 데이터 볼륨을 지우므로 데이터 보존이 필요할 때 사용하지 않습니다.

환경을 정지하거나 다시 시작할 때는 다음 명령을 사용합니다.

```powershell
docker compose stop primary runner
docker compose up -d --wait primary runner
```

## 다음 단계

1. Standby를 추가하고 실제 데이터 복제와 읽기 전용 상태를 검증합니다.
2. Primary 강제 종료, 안전한 승격, 클라이언트 재연결을 구현합니다.
3. 동기·비동기 복제와 멱등성, 기존 Primary 재편입을 검증합니다.
4. Base Backup과 WAL로 PITR을 수행하고 실측 결과를 정리합니다.

각 단계의 코드와 검증 결과를 확인한 뒤 다음 단계로 진행합니다.
