# PostgreSQL High Availability & Disaster Recovery Lab

PostgreSQL 장애 상황에서 데이터 유실 범위와 쓰기 복구 시간을 직접 측정하는 DBA 포트폴리오 프로젝트입니다.

Primary와 Standby의 복제부터 시작해 장애 전환, 재시도 정합성, 기존 Primary 재편입, WAL 기반 PITR을 단계별로 구현할 계획입니다. 단일 PC의 Docker 환경에서 실험하며 운영 환경의 고가용성이나 무손실을 보장하는 프로젝트는 아닙니다.

## 현재 단계

Python 실행용 `runner` 컨테이너를 준비했습니다. PostgreSQL 서버와 복제 구성은 다음 단계에서 추가합니다.

| 구성 요소 | 버전 |
|---|---|
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

`runner`가 실행 중이고 Python `3.12.11`, psycopg `3.2.9`, pytest `8.4.1`이 출력되면 환경 준비가 끝납니다. 현재 단계의 검증은 Compose 설정과 실행 컨테이너, 패키지 버전 확인까지입니다.

환경을 정지하거나 다시 시작할 때는 다음 명령을 사용합니다.

```powershell
docker compose stop runner
docker compose start runner
```

## 다음 단계

1. PostgreSQL Primary 한 대를 실행하고 SQL 접속을 확인합니다.
2. Standby를 추가하고 실제 데이터 복제와 읽기 전용 상태를 검증합니다.
3. Primary 강제 종료, 안전한 승격, 클라이언트 재연결을 구현합니다.
4. 동기·비동기 복제와 멱등성, 기존 Primary 재편입을 검증합니다.
5. Base Backup과 WAL로 PITR을 수행하고 실측 결과를 정리합니다.

각 단계의 코드와 검증 결과를 확인한 뒤 다음 단계로 진행합니다.
