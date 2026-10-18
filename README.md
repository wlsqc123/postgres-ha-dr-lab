# PostgreSQL High Availability & Disaster Recovery Lab

PostgreSQL 장애 상황에서 데이터 유실 범위와 쓰기 복구 시간을 직접 측정하는 DBA 포트폴리오 프로젝트입니다.

**[포트폴리오 본문 — 장애를 어떻게 만들고 복구했는가](docs/portfolio.md)**에서 구현 이유, 실제 명령, 판정 기준과 실측 결과를 순서대로 설명합니다. 이 README는 단계별 환경 구성과 실행 방법을 다룹니다.

[면접 설명 요약](docs/interview.md) · [최종 점검 기록](experiments/final-verification.json)

Primary와 Standby의 복제부터 장애 전환, 재시도 정합성, 기존 Primary 재편입, 동기·비동기 비교와 WAL 기반 PITR을 단계별로 검증했습니다. 단일 PC의 Docker 환경에서 실험하며 운영 환경의 고가용성이나 무손실을 보장하는 프로젝트는 아닙니다.

## 현재 단계

물리 복제, 장애 전환, 재편입, 동기·비동기 비교와 PITR 검증을 마쳤습니다. 현재 서비스 이름 `standby`가 새 Primary이며 `primary`는 새 백업으로 재구성한 읽기 전용 Standby입니다. 장애 당시 디스크는 차단 표식과 함께 별도로 보존했습니다.

재편입 이후에는 모든 시작·정지 작업에 `-f compose.yaml -f compose.rejoin.yaml`을 함께 지정합니다. 아래 초기 구축 명령은 장애 전환 이전 단계의 재현용입니다.

장애 전후 데이터를 비교할 기준으로 계좌·원장과 요청 ID 기반 이체도 추가했습니다. 같은 요청을 재전송해도 한 번만 반영되는지, 실패한 거래가 부분 반영되지 않는지 검증합니다.

| 구성 요소 | 버전 |
|---|---|
| PostgreSQL | 18.0-bookworm |
| runner Python | 3.12.11 |
| psycopg | 3.2.9 |
| pytest | 8.4.1 |

Python 패키지는 컨테이너 안에 설치합니다. 기본 복제·원장 검증은 호스트 Python 없이 실행합니다. 장애 전환 제어에는 호스트 Python 3.11 이상이 필요하며 추가 패키지는 쓰지 않습니다.

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

## Standby 구성과 복제 검증

Primary와 runner가 준비된 상태에서 복제 전용 계정과 slot을 만든 뒤 Standby를 시작합니다.

```powershell
docker compose up -d --build --wait primary runner
docker compose exec -T runner python scripts/prepare_replication.py
docker compose up -d --wait standby
docker compose exec -T runner python scripts/check_replication.py
```

복제 계정은 일반 관리자와 분리한 `replicator`입니다. `.env`의 `REPLICATION_PASSWORD`를 쓰며 기본값은 로컬 실습용 `local-repl`입니다. 이미 있는 계정의 권한이나 암호를 준비 스크립트가 임의로 덮어쓰지는 않습니다. 암호를 변경할 때는 DB 계정과 연결 설정을 함께 관리해야 합니다.

Standby는 별도 `standby_stage2` 볼륨을 사용합니다. 최초 시작 시 빈 디렉터리에서만 `pg_basebackup -R -X stream -S standby_stage2`를 수행합니다. `-R`이 `standby.signal`, `primary_conninfo`, slot 연결 설정을 생성합니다. 이후에는 같은 데이터를 재사용하며, 불완전한 데이터 디렉터리나 `standby.signal`이 없는 복사본은 자동 삭제하거나 Primary로 시작하지 않고 오류로 종료합니다. [PostgreSQL 18 pg_basebackup](https://www.postgresql.org/docs/18/app-pgbasebackup.html)

`check_replication.py`는 매번 고유 UUID와 고정 문자열을 Primary에 커밋한 뒤, Standby의 조회 결과를 그 기대값과 비교합니다. 두 서버의 시스템 식별자가 같은지도 확인합니다. 이어서 Standby에 실제 INSERT를 시도하고 SQLSTATE `25006`으로 거부되는지 검사합니다. 컨테이너 실행 여부만으로 성공을 판정하지 않습니다.

| 확인 항목 | 기대 결과 |
|---|---|
| Primary `pg_is_in_recovery()` | `false` |
| Standby `pg_is_in_recovery()` / read-only | `true` / `on` |
| Primary `pg_stat_replication` | `state=streaming`, `sync_state=async` |
| Standby `pg_stat_wal_receiver` | `status=streaming` |
| 복제 slot | `standby_stage2`, `active=true`, WAL 유실 상태 아님 |
| 새 요청 데이터 | 생성한 UUID와 문자열이 Standby 조회값과 일치 |
| Standby INSERT | 읽기 전용 트랜잭션 오류로 거부 |

결과에는 WAL sent/write/flush/replay LSN과 Standby 수신·재생 LSN, slot 상태를 출력합니다. [이번 단계의 실제 결과](experiments/stage2-streaming.json)를 함께 보관합니다.

slot은 Standby가 받지 못한 WAL을 Primary에 남기므로 오래 중단되면 디스크가 차오를 수 있습니다. 실습 설정은 `max_slot_wal_keep_size=256MB`로 제한했습니다. 이 값은 전체 WAL 디스크 사용량의 엄격한 상한이 아닙니다. `wal_status`, `safe_wal_size`를 관측하고, 필요한 WAL이 제거돼 slot이 `lost`가 되면 새 Base Backup으로 구성해야 합니다. 비동기 복제에서 한 번 데이터가 일치했다고 장애 시 무손실을 보장하지는 않습니다. [PostgreSQL 18 Streaming Replication과 Slots](https://www.postgresql.org/docs/18/warm-standby.html)

## 계좌·원장과 재시도 검증

복제가 구성된 상태에서 실행합니다. `.env`의 `APP_PASSWORD`는 비즈니스 요청 전용 계정 `ledger_app`의 암호이며 기본값은 실습용 공개 값 `local-app`입니다. 기존 계정의 암호를 설정 스크립트가 임의로 바꾸지는 않습니다.

```powershell
docker compose up -d runner
docker compose exec -T runner python -m ledger.setup
docker compose exec -T runner python -m ledger.check
docker compose exec -T runner python -m pytest -q checks/test_ledger.py
```

`accounts`는 계좌 잔액, `request_history`는 요청 ID와 이체 내용, `ledger_entries`는 계좌별 증감 기록입니다. 하나의 이체에서 요청 이력, 출금·입금 잔액, 두 원장을 하나의 트랜잭션으로 커밋합니다. `ledger.model.transfer`는 직접 COMMIT을 완료한 뒤 결과를 반환하며, 다른 트랜잭션 안에서 호출해 아직 커밋하지 않은 결과를 성공으로 반환하지 않습니다.

| 검증 | 기대 동작 |
|---|---|
| 최초 이체 | `new`, 이력 한 건과 원장 두 건 반영 |
| 같은 ID·같은 내용 재시도 | `existing`, 추가 잔액 변동 없음 |
| 같은 ID·다른 금액 | `PayloadMismatch`로 거부 |
| 잔액 부족 | CHECK 오류로 거래 전체 롤백, 실패 요청 이력도 없음 |
| 두 연결의 같은 ID 동시 전송 | `new` 한 번, `existing` 한 번 |
| 서로 반대 방향의 동시 이체 | 두 거래 모두 커밋하고 각 원장·잔액 검증 |
| Standby | 같은 요청·원장·잔액으로 복제 완료 |

요청 ID의 유일성 제약과 저장된 payload 비교로 중복 효과를 막습니다. 이체는 READ COMMITTED에서 수행하고, 계좌 ID 순서대로 `FOR NO KEY UPDATE` 잠금을 얻습니다. 잔액 변경에는 키 변경 잠금이 필요하지 않으며, 이 잠금은 외래키 검사에 쓰이는 KEY SHARE와 충돌하지 않습니다. 이 선택과 반대 방향 이체 검증으로 계좌 잠금 시 불필요한 충돌을 줄였습니다. [PostgreSQL 18 행 잠금](https://www.postgresql.org/docs/18/explicit-locking.html#LOCKING-ROWS)

검증마다 고유한 계좌 두 개를 만들고 각 10,000에서 시작합니다. 기존 계좌와 거래는 지우거나 초기화하지 않습니다. 성공할 요청 네 건의 payload와 초기 잔액을 DB 작업 전에 `inputs.json`으로 기록합니다. Python 검증기는 그 입력만으로 기대 잔액과 원장을 계산하고, 해당 계좌 범위의 DB 전체 행과 비교합니다. DB에서 조회한 합계를 기대값으로 다시 사용하지 않습니다. 최종 기대값은 요청 4건, 원장 8건, 잔액 9,625와 10,375입니다. 조회는 REPEATABLE READ의 동일 snapshot에서 수행합니다.

클라이언트 기록 `client.jsonl`에는 요청 전 `intent`, COMMIT 응답 후 `acknowledged`, 확인된 거부는 `rejected`로 남깁니다. 각 기록은 flush/fsync합니다. 이는 DB 효과의 멱등성 검증이며 Exactly-Once Delivery를 주장하지 않습니다. 장애 전환 실험에서는 연결 실패와 거래 결과 불명확 상태도 구분합니다.

실행 원본은 `results/ledger-<UTC 실행ID>/`에 보존하고, 실패하면 traceback과 0이 아닌 종료 코드를 남깁니다. 반복 실행은 새 계좌 범위를 사용합니다. [실제 결과](experiments/stage3-ledger.json), [입력](experiments/stage3-ledger/inputs.json), [클라이언트 기록](experiments/stage3-ledger/client.jsonl), [Primary](experiments/stage3-ledger/primary.json), [Standby](experiments/stage3-ledger/standby.json), [단위 테스트](experiments/stage3-ledger/pytest.log)를 공개합니다.

아래 시작·정지 명령과 앞선 Primary·복제 검증 명령은 장애 전환 이전의 구성에서 사용합니다. 승격 이후에는 기존 Primary를 시작하지 않습니다.

```powershell
docker compose stop standby primary runner
docker compose up -d --wait primary runner standby
```

## Primary 장애 전환

원장 초기화까지 마친 정상 Primary/Standby 쌍에서 한 번 실행합니다. 기존 실험 환경은 이미 승격했으므로 재편입 전에는 다시 실행하지 않습니다.

```powershell
docker compose up -d --wait primary runner standby
docker compose exec -T runner python -m pytest -q checks/test_ledger.py checks/test_failover.py
python -m ledger.failover --confirm-primary-kill
```

호스트 제어기가 기존 Primary의 SIGKILL 종료와 `restart=no`를 확인하고 데이터 디렉터리에 `lab.fenced` 표식을 남긴 뒤 승격합니다. Primary 시작 스크립트는 이 표식이 있으면 PostgreSQL을 실행하지 않습니다. 단일 PC의 수동 실험용 차단이며 운영 환경의 STONITH나 분산 합의를 대신하지 않습니다.

승격 직후 재시도와 새 쓰기에 앞서 승인 요청의 생존 여부를 저장합니다. 이후 쓰기 확인, 앱 연결 전환, 같은 요청 ID 재시도, 독립적인 기대값 비교를 수행합니다. [실험 설명](docs/stage4-failover.md), [실측 결과](experiments/stage4-failover.json), [실행 기록](experiments/stage4-failover/)을 공개합니다.

이번 실행은 승인 159건 중 유실 0건, 관측 복구 시간 13.828초, 최종 요청 205건과 원장 410건의 정합성 검증을 통과했습니다. 한 번의 비동기 실험에서 유실이 없었다고 무손실을 보장하지 않습니다.

4단계 종료 시 구 Primary는 차단된 정지 상태였습니다. 다음 재편입 단계는 그 디스크를 보존하고 새 볼륨으로 읽기 전용 Standby를 구성합니다.

## 기존 Primary 재편입

4단계의 장애 전환을 마친 상태에서 한 번 실행합니다. 호스트 제어기는 구 Primary의 정지·차단 상태를 확인하고 새 Primary의 Base Backup을 별도 볼륨에 받습니다. 이 명령은 이미 재편입한 현재 환경에서 재실행하지 않습니다.

```powershell
python -m ledger.rejoin --confirm-rejoin
```

기존 `primary_stage1` 볼륨은 보존하고 새 `primary_stage5` 볼륨을 사용합니다. 새 Primary의 복제 slot `primary_stage5`와 `pg_basebackup -R -X stream`으로 Standby를 구성합니다. 이전 단계의 요청 205건에 새 이체 1건을 추가한 결과, 요청 206건·원장 412건과 잔액이 두 서버에서 일치했습니다. 같은 요청 재시도, Standby 쓰기 거부, Standby 재시작 후 복제 유지도 확인했습니다. [설명](docs/stage5-rejoin.md), [실측 결과](experiments/stage5-rejoin.json), [실행 기록](experiments/stage5-rejoin/)을 공개합니다.

재편입 이후 상태 확인과 재시작에는 다음 명령을 사용합니다. runner의 기본 접속 대상도 새 Primary인 standby로 설정합니다. 서비스 이름과 DB 역할이 바뀌었으며 원래 역할로 되돌리는 failback은 수행하지 않았습니다.

```powershell
docker compose -f compose.yaml -f compose.rejoin.yaml ps
docker compose -f compose.yaml -f compose.rejoin.yaml exec standby psql -U postgres -d lab
docker compose -f compose.yaml -f compose.rejoin.yaml exec primary psql -U postgres -d lab
docker compose -f compose.yaml -f compose.rejoin.yaml stop primary standby runner
docker compose -f compose.yaml -f compose.rejoin.yaml up -d --wait standby runner primary
```

실습 중인 현재 환경을 유지하려면 마지막 두 명령을 실행할 필요는 없습니다. 기본 compose.yaml만으로 다시 시작하면 보존된 구 디스크를 가리키며 차단 장치가 시작을 거부합니다. 차단 표식을 지우거나 기존 볼륨을 삭제하지 않습니다.

## 동기·비동기 복제 비교

기존 재편입 환경을 보존하고 같은 프로젝트의 별도 DB 두 쌍에서 비교했습니다. 각 300건의 이체 승인 지연 p95는 비동기 21.545ms, 동기 28.664ms였습니다. Standby를 중단하면 비동기는 승인을 계속했고 동기는 SyncRep에서 대기했습니다. 이 상태에서 Primary를 종료하고 승격했을 때 승인 유실은 비동기 10/321건, 동기 0/311건이었습니다. 복제 단절을 의도적으로 만든 조건이며 일반적인 장애 유실률을 뜻하지 않습니다.

```powershell
python -m ledger.compare --confirm-comparison-faults
```

비교용 컨테이너·볼륨이 없는 초기 상태에서 한 번 실행합니다. 끝나면 비교용 DB만 정지하고 볼륨은 보존합니다. [측정 조건](docs/stage6-replication-compare.md)과 [실측 결과](experiments/stage6-compare.json)를 확인할 수 있습니다.

## PITR 복구

별도 DB에서 Base Backup 이후 정상 거래를 추가하고 DB의 시각을 목표로 기록한 뒤 계좌·요청·원장을 삭제했습니다. 백업과 보관 WAL을 별도 볼륨에 복원해 목표 시점에서 정지시키고, 정상 요청 3건·원장 6건과 잔액을 검증했습니다. 목표 이후 거래와 삭제는 반영되지 않았고, 필수 WAL을 제공하지 않은 별도 복구는 명시적으로 실패했습니다.

```powershell
python -m ledger.pitr --confirm-isolated-delete
```

stage7 컨테이너·볼륨이 없는 초기 상태에서 한 번 실행합니다. 기존 재편입 환경은 유지하며 실험용 볼륨도 삭제하지 않습니다. [복구 설정과 판정 기준](docs/stage7-pitr.md), [실측 결과](experiments/stage7-pitr.json)를 확인할 수 있습니다.
