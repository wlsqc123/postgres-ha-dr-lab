# 기존 Primary를 Standby로 재편입

2026-10-18 실행 `20261018T123812Z-751f42ae`에서 새 Base Backup 방식으로 재편입했습니다. pg_rewind나 원래 역할로 돌리는 failback은 수행하지 않았습니다.

## 데이터 보존과 시작 조건

장애 당시 볼륨 `postgres-ha-dr-lab_primary_stage1`은 lab.fenced 표식과 함께 보존합니다. 구 Primary 컨테이너가 멈췄고 restart=no인지 확인한 뒤 재편입 설정을 적용합니다. 새 볼륨 `postgres-ha-dr-lab_primary_stage5`만 재구성 대상으로 사용합니다. 볼륨을 삭제하거나 기존 데이터 디렉터리를 덮어쓰지 않습니다.

새 Primary는 서비스 이름 standby입니다. 승격 이후 standby.signal이 사라졌으므로 기존 Standby 전용 시작 스크립트로는 재시작할 수 없습니다. compose.rejoin.yaml에서 기존 PG_VERSION이 있고 standby.signal과 차단 표식이 없는 경우에만 Primary로 시작하도록 설정합니다. 이번 실행에서는 꺼져 있던 Docker를 복구한 뒤 이 설정으로 새 Primary를 시작했습니다.

재편입 대상인 서비스 primary는 기존 Standby 시작 스크립트를 사용합니다. upstream=standby, application_name=primary_stage5, slot=primary_stage5로 접속합니다. 빈 데이터 디렉터리에서 pg_basebackup --create-slot -R -X stream --checkpoint=fast --no-clean을 실행합니다. 기존 slot이 있거나 불완전한 데이터가 남아 있으면 중단하며 임의로 초기화하지 않습니다. 실패한 백업도 조사할 수 있도록 남깁니다. `-R`이 standby.signal과 연결·slot 설정을 생성합니다. [PostgreSQL 18 pg_basebackup](https://www.postgresql.org/docs/18/app-pgbasebackup.html)

## 검증 결과

| 항목 | 결과 |
|---|---|
| 새 Primary / 재편입 Standby | standby / primary |
| 재편입 방식 | 별도 볼륨의 새 Base Backup |
| 백업 시작 명령부터 healthy 확인 | 5.859초 |
| 시스템 식별자 | 두 서버 일치 |
| 수신 상태 | streaming, 송신 호스트 standby |
| 송신 상태 / 동기 상태 | streaming / async |
| 복제 slot | primary_stage5, active=true, lost 아님 |
| 기존 거래 | 이전 실험의 입력으로 계산한 205건 검증 |
| 새 거래 / 같은 ID 재시도 | new / existing |
| 최종 요청 / 원장 | 206 / 412 |
| 최종 잔액 | 999,778 / 1,000,222 |
| Standby 쓰기 | SQLSTATE 25006으로 거부 |
| Standby 재시작 | 복구 모드·streaming·동일 데이터 유지 |
| 테스트 | 22개 통과 |

5.859초는 호스트 monotonic 시계로 측정한 Compose 실행과 healthcheck 확인을 포함합니다. 백업 자체 처리 시간이나 서비스 전체 복구 시간을 뜻하지 않습니다.

기대값은 이전 장애 전환 단계의 초기 잔액·요청 목록에 새 요청을 더해 독립적으로 계산합니다. DB 합계를 기대값으로 재사용하지 않습니다. Primary와 Standby 각각의 동일 계좌 범위에서 이력·원장·잔액을 비교합니다. 새 거래는 금액 17이며 재시도에서 추가 잔액 변동이 없어야 합니다. 쓰기 거부 검증은 recovery 상태를 먼저 확인하고, 예상과 달리 쓰기가 가능해도 트랜잭션을 롤백합니다.

## 실행과 실패 처리

4단계 종료 직후 호스트에서 `python -m ledger.rejoin --confirm-rejoin`을 한 번 실행합니다. 단계별 명령, 입력, DB 상태, snapshot과 로그를 results/rejoin-실행ID 아래에 저장합니다. 실패 시 failure.json, success=false와 0이 아닌 종료 코드를 남깁니다. 이전 성공 결과와 scoped 데이터가 변경된 환경, 이미 재편입한 환경은 재실행 대상이 아닙니다. 실패 시 볼륨이나 slot을 자동 삭제해 다시 시도하지 않습니다.

[`result.json`](../experiments/stage5-rejoin/result.json), [`before.json`](../experiments/stage5-rejoin/before.json), [`after.json`](../experiments/stage5-rejoin/after.json), [`source-snapshot.json`](../experiments/stage5-rejoin/source-snapshot.json), [`replica-snapshot.json`](../experiments/stage5-rejoin/replica-snapshot.json), [`readonly.json`](../experiments/stage5-rejoin/readonly.json)에서 판정 근거를 확인합니다. 재시작 후 실제 streaming 상태와 snapshot 조회는 commands.jsonl에도 기록합니다.

이후 시작·정지에는 반드시 `docker compose -f compose.yaml -f compose.rejoin.yaml`을 사용합니다. runner의 PGHOST도 standby로 바꿔 기본 연결이 새 Primary를 향하도록 합니다. 초기 compose.yaml만 쓰면 보존된 구 디스크를 가리킵니다. 원래 Primary의 장애 데이터는 아직 차단 상태이며 운영자가 표식을 지우거나 별도 방식으로 시작하지 않습니다. 비동기 복제를 유지하며 동기 복제 비교와 PITR은 다음 단계입니다.
