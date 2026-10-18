# PostgreSQL 장애 전환과 기존 Primary 재편입

## 무엇을 확인하려고 했는가

Primary가 갑자기 종료됐을 때 Standby를 승격하는 것만으로 복구가 끝났다고 볼 수 있는지 확인하고 싶었다. DB가 쓰기를 받더라도 이미 성공 응답을 보낸 거래가 사라졌거나, 재시도 때문에 잔액이 두 번 바뀌었다면 정상 복구라고 보기 어렵다.

그래서 비동기 물리 스트리밍 복제를 구성한 뒤 계좌 이체를 계속 보내면서 Primary를 강제 종료했다. 기존 Primary를 차단한 뒤 Standby를 승격하고, 재시도 전에 승인 거래가 얼마나 남았는지 측정했다. 이후 클라이언트 연결을 전환하고 같은 요청 ID로 재시도해 최종 이력·원장·잔액을 검증했다. 마지막으로 장애가 났던 Primary를 새 백업으로 재구성해 Standby로 재편입했다.

단일 PC의 Docker 환경에서 수행한 실험이다. 자동 장애 전환이나 운영 환경의 무손실을 구현했다는 의미는 아니다. 완료한 범위는 비동기 복제, 장애 전환, 재시도 정합성, 새 Base Backup을 이용한 재편입이다.

## 환경과 데이터 모델

PostgreSQL 18.0 컨테이너 두 대와 Python 작업자를 내부 Docker 네트워크로 연결했다. 초기 서비스 이름 primary가 Primary, standby가 읽기 전용 Standby였다. 호스트에는 DB 포트를 공개하지 않았다. 작업자는 Python 3.12.11과 psycopg 3.2.9를 사용했다.

Standby는 pg_basebackup으로 초기 데이터를 복사한 뒤 WAL을 받아 적용했다. Primary의 pg_stat_replication에서 streaming과 async를 확인했고, Standby의 pg_stat_wal_receiver에서도 streaming을 확인했다. 새 UUID와 문자열을 Primary에 커밋한 뒤 Standby에서 같은 값을 조회했다. Standby에 쓰기를 시도했을 때 SQLSTATE 25006으로 거부되는지도 확인했다.

장애 실험용 데이터는 계좌, 요청 이력, 원장으로 구성했다. 하나의 이체에서 요청 이력 한 건, 출금·입금 잔액, 원장 두 건을 하나의 트랜잭션으로 커밋했다. 요청 ID에 유일성 제약을 두고, 같은 ID가 다시 들어오면 저장된 출금 계좌·입금 계좌·금액이 같은지 검사했다. 같은 내용이면 existing을 반환하고 잔액을 다시 변경하지 않는다. 내용이 다르면 거부한다.

두 계좌의 잠금은 계좌 ID 순서대로 FOR NO KEY UPDATE로 획득했다. 잔액만 변경하므로 키 변경 잠금은 필요하지 않으며, 외래키 검사의 KEY SHARE와 불필요한 충돌을 줄이려는 선택이었다. 같은 ID의 동시 전송과 서로 반대 방향의 동시 이체도 별도로 검증했다.

구현은 [복제 설정](../compose.yaml), [Standby 초기화](../scripts/standby_entrypoint.sh), [이체 트랜잭션](../ledger/model.py)에서 확인할 수 있다.

## 1. 성공 응답을 비교할 수 있도록 클라이언트 기록을 남겼다

장애 후 DB에 남은 거래만 조회하면, 이미 사라진 거래를 찾을 기준이 없다. DB와 별개로 클라이언트가 무엇을 요청했고 어떤 응답을 받았는지 남겼다.

작업자는 요청을 보내기 전에 intent를 기록하고, 이체 함수가 COMMIT을 완료한 뒤 반환하면 acknowledged를 기록했다. 각각 flush와 fsync를 수행했다. 연결 자체가 실패한 경우와, 거래 수행 중 연결이 끊겨 결과를 확정할 수 없는 uncertain 상태를 구분했다. 결과 기록이 아직 없는 intent도 생존 판정 시 보수적으로 결과 불명확에 포함했다.

요청 ID와 payload, 접속 호스트, 결과, 지연 시간을 기록했다. 작업자는 초당 20건을 목표로 순차 요청했으며, 요청마다 새 연결을 만들었다. 이 값은 실제 초당 처리량을 보장하는 수치가 아니다.

[작업자 코드](../ledger/workload.py), [클라이언트 기록](../experiments/stage4-failover/client.jsonl)

## 2. 거래 부하 중 Primary에 SIGKILL을 보냈다

정상 거래가 발생하는 것을 확인한 뒤 다음 명령을 실행했다.

```powershell
docker compose kill -s SIGKILL primary
```

컨테이너 주 프로세스인 PostgreSQL에 SIGKILL을 보내 정상 종료 절차를 기다리지 않고 종료했다. 실제 컨테이너는 종료 코드 137로 멈췄고, 작업자에서도 DB 연결 실패가 발생했다. 장애 명령을 실행했다는 사실만으로 감지를 판정하지 않고 실제 클라이언트 실패 기록까지 확인했다.

이 실험은 DB 프로세스 비정상 종료를 재현했다. 호스트 전원 차단이나 네트워크 분할을 재현한 실험은 아니다.

[장애 제어 코드](../ledger/failover.py), [첫 클라이언트 실패](../experiments/stage4-failover/first-client-failure.json)

## 3. 기존 Primary의 정지와 재시작 차단을 확인했다

승격 전에 기존 Primary가 계속 쓰기를 받을 가능성부터 차단했다. docker inspect 결과를 읽어 해당 컨테이너가 이 Compose 프로젝트의 Primary인지 확인하고, 컨테이너 ID가 바뀌지 않았는지도 검사했다.

승격 조건은 컨테이너가 정지 상태이고, 일시 정지나 재시작 중이 아니며, 재시작 정책이 no인 것이다. restart=no는 Compose에 미리 설정했다. 조건을 만족하지 않으면 승격하지 않도록 했다.

추가로 정지한 Primary의 데이터 볼륨에 /data/pgdata/lab.fenced 표식을 기록했다. Primary 시작 스크립트는 이 파일이 있으면 PostgreSQL 실행을 거부한다. 표식은 PostgreSQL을 실행하지 않는 별도 임시 컨테이너에서 기록했다.

이는 실습에서 실수로 구 Primary를 다시 켜는 것을 막는 장치다. 운영자가 시작 스크립트를 우회하는 상황까지 강제 차단하지는 못하며, 운영 환경의 STONITH나 분산 합의를 대신하지 않는다.

[차단 판정](../ledger/journal.py), [시작 차단 스크립트](../scripts/primary_entrypoint.sh), [실제 차단 기록](../experiments/stage4-failover/fencing.json)

## 4. Standby에 승격을 요청하고 완료 여부를 따로 확인했다

새 컨테이너를 만드는 대신 실행 중인 Standby에 관리자 연결을 열어 다음 SQL을 실행했다.

```sql
SELECT pg_promote(false);
```

false는 승격 완료까지 기다리지 않고 요청 결과를 반환하도록 지정한 것이다. 따라서 반환값만으로 완료를 판정하지 않았다. 다음 SQL을 반복 조회해 true였던 값이 false로 바뀌는 것을 확인했다.

```sql
SELECT pg_is_in_recovery();
```

이후 체크포인트를 수행하고 Timeline 증가도 확인했다.

```sql
CHECKPOINT;
SELECT timeline_id FROM pg_control_checkpoint();
```

실행 결과 Timeline은 1에서 2로 증가했다. 승격을 요청한 시점부터 recovery=false를 확인한 시점까지는 1.782초였다.

[SQL 실행 코드](../ledger/node.py), [승격 후 DB 상태](../experiments/stage4-failover/promoted-state.json)

## 5. 재시도로 유실을 가리기 전에 승인 거래의 생존을 측정했다

승격 직후 새 거래나 재시도를 보내기 전에 DB snapshot을 저장했다. 클라이언트의 승인 요청 ID와 새 Primary의 요청 이력을 비교해 승인됐지만 존재하지 않는 거래를 찾았다.

재시도를 먼저 하면 사라진 거래가 다시 생성돼 최초 유실을 놓칠 수 있다. 그래서 재시도 전 생존 여부와 재시도 후 최종 정합성을 별도 결과로 남겼다.

이번 실행에서는 승인 159건이 모두 남아 있었고 승인 유실은 0건이었다. 결과 불명확 요청은 1건이었으며 이 요청은 승격 직후 DB에 없었다. 한 번의 실행에서 유실이 없었다고 비동기 복제가 무손실이라고 결론 내리지는 않았다.

[재시도 전 판정](../experiments/stage4-failover/pre-retry-survival.json), [승격 직후 snapshot](../experiments/stage4-failover/immediate-snapshot.json)

## 6. 실제 쓰기를 확인한 뒤 클라이언트 연결을 전환했다

새 Primary에 별도의 이체를 보내 COMMIT이 가능한지 확인했다. 그다음 작업자가 읽는 route.json의 호스트를 primary에서 standby로 원자적으로 변경했다.

```json
{"host": "standby"}
```

서비스 이름은 standby지만 이 시점의 DB 역할은 Primary다. 작업자는 매 요청마다 접속 대상을 다시 읽고 연결한다. 이번 전환은 파일 기반 클라이언트 라우팅이며 DNS, 프록시, 커넥션 풀을 통한 전환을 검증한 것은 아니다.

장애 명령 요청부터 새 Primary에서 앱의 첫 거래 성공을 관측하기까지 13.828초가 걸렸다. 호스트 monotonic 시계로 측정했으며, Docker 호출, 차단, snapshot 수집, 쓰기 확인과 관측 대기 시간을 포함한다. DB 단독 승격 시간이나 운영 RTO 보장과 구분한다.

[이벤트 시각](../experiments/stage4-failover/events.json), [새 Primary에서의 첫 앱 성공](../experiments/stage4-failover/first-application-success.json)

## 7. 같은 요청 ID로 재시도하고 독립적인 기대값과 비교했다

작업자를 정지한 뒤 기록된 모든 intent를 같은 ID와 payload로 새 Primary에 다시 보냈다. 이미 존재하면 existing, 존재하지 않으면 new로 처리했다.

작업자 요청은 총 204건이었다. 재시도에서는 existing 201건과 new 3건이 나왔다. new 3건을 승인 유실 3건으로 해석하지 않았다. 연결 실패 등으로 승인되지 않았고 DB에도 없었던 요청을 새로 반영한 경우가 포함되기 때문이다.

별도 쓰기 확인 거래 1건을 포함한 최종 기대 요청은 205건이었다. 초기 계좌 잔액과 요청 내용만으로 Python 검증기가 기대 이력, 원장, 잔액을 계산했다. DB에서 조회한 합계를 다시 기대값으로 사용하지 않았다. 실제 요청 205건, 원장 410건, 잔액 999,795와 1,000,205가 기대값과 일치했다.

이 검증은 요청 재시도에 따른 DB 효과의 멱등성을 확인한다. Exactly-Once Delivery를 구현했다고 주장하지 않는다.

[독립 검증기](../ledger/oracle.py), [재시도 기록](../experiments/stage4-failover/retries.jsonl), [최종 검증](../experiments/stage4-failover/final-verification.json)

## 8. 기존 Primary를 새 백업으로 재구성해 Standby로 붙였다

승격된 서버가 새로운 Timeline에서 쓰기를 받기 시작했으므로 기존 Primary를 그대로 쓰기 가능한 상태로 다시 켜지 않았다. 새 Primary를 기준으로 데이터를 맞춘 뒤 Standby로 시작했다.

이번에는 pg_rewind 대신 전체 Base Backup을 선택했다. 실습 데이터 규모에서 장애 디스크를 보존하면서 새 Primary의 전체 복사본으로 재구성하는 절차를 먼저 검증하려는 선택이었다. 데이터가 크면 복사량과 시간이 늘어나는 방식이다.

기존 primary_stage1 볼륨은 보존하고 별도 primary_stage5 볼륨에 백업을 받았다. 시작 스크립트가 실행한 명령을 이 실행의 설정값으로 풀면 다음과 같다. 이 명령은 빈 대상 디렉터리에서 한 번 실행하는 절차이며, 현재 재편입이 완료된 환경에 다시 실행하는 명령이 아니다.

```bash
pg_basebackup \
  --dbname='host=standby user=replicator application_name=primary_stage5' \
  -D "$PGDATA" -R -X stream -S primary_stage5 \
  --create-slot --checkpoint=fast --no-clean
```

- -R은 standby.signal과 복제 연결·slot 설정을 생성한다.
- -X stream은 백업에 필요한 WAL을 별도 연결로 함께 수신한다.
- --create-slot과 -S는 사용할 물리 복제 slot을 생성하고 지정한다. 같은 slot을 Standby도 이어서 사용한다.
- --no-clean은 백업 실패 시 조사할 파일을 자동 정리하지 않도록 한다.

데이터 디렉터리가 불완전하게 채워져 있거나 standby.signal이 없으면 자동으로 지우거나 Primary로 시작하지 않고 중단하도록 했다. slot은 지연된 Standby에 필요한 WAL을 보존하는 데 사용했지만 무제한 보존을 가정하지 않았다. max_slot_wal_keep_size는 256MB로 설정했고 active와 wal_status를 확인했다. 이 설정은 전체 WAL 디스크 사용량의 엄격한 상한이 아니다.

재편입 후 recovery=true, 수신·송신 streaming, slot 활성화를 확인했다. 이전 요청 205건에 새 이체 1건을 추가하고 같은 ID로 다시 보내 new/existing을 확인했다. 두 서버의 요청 206건, 원장 412건, 잔액 999,778과 1,000,222가 모두 일치했다. Standby 쓰기가 SQLSTATE 25006으로 거부되는지, Standby 재시작 후에도 복제와 데이터가 유지되는지도 확인했다. 관련 테스트는 22개를 통과했다.

[재편입 제어 코드](../ledger/rejoin.py), [재편입 설정](../compose.rejoin.yaml), [실측 결과](../experiments/stage5-rejoin.json), [재시작 후 상태](../experiments/stage5-rejoin/after-restart.json)

## 결과를 어떻게 해석했는가

| 질문 | 이번 실험에서 확인한 답 |
|---|---|
| Primary 프로세스가 갑자기 종료되면 승격 가능한가? | 기존 노드 차단 확인 후 승격했고 Timeline 1 → 2를 확인했다 |
| 이미 승인한 거래가 남았는가? | 재시도 전 승인 159건 모두 존재했다 |
| 재시도로 중복 반영됐는가? | ID와 payload를 검사하고 기대 이력·원장·잔액 일치를 확인했다 |
| 앱의 쓰기는 언제 복구됐는가? | 장애 요청부터 첫 성공 관측까지 13.828초였다 |
| 기존 노드를 복제 구성에 다시 넣을 수 있는가? | 새 백업으로 구성한 Standby의 복제·쓰기 거부·재시작을 확인했다 |

현재 서비스 이름 standby가 Primary, primary가 재편입한 Standby다. runner의 기본 접속 대상도 standby로 바꿨다. 기존 장애 디스크는 차단 상태로 보존한다. 원래 서버로 Primary 역할을 되돌리는 failback은 수행하지 않았다.

앞으로는 동기·비동기 복제의 승인 지연과 장애 시 유실을 비교하고, Base Backup과 보관 WAL을 이용한 PITR을 검증할 계획이다. pg_rewind, 네트워크 분할, 자동 장애 전환, 호스트 전체 장애는 아직 검증하지 않았다.
