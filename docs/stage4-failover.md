# Primary 강제 종료와 수동 장애 전환

실행 ID는 `20261017T102227Z-399b090b`입니다. PostgreSQL 18.0 비동기 물리 복제, 단일 PC Docker 환경에서 측정했습니다. 테스트 16개와 실제 복제 상태를 확인한 뒤 장애를 주입했습니다.

## 절차와 검증 기준

새 계좌 두 개를 각각 1,000,000으로 만들고 초기 잔액과 요청 내용을 파일에 남깁니다. 작업자는 초당 20건을 목표로 순차 이체하며 매 요청마다 연결합니다. 실제 처리율을 보장하는 부하 생성기는 아닙니다. 요청 전 intent와 COMMIT 응답 후 결과를 각각 fsync합니다.

제어기는 Primary를 SIGKILL로 종료하고 컨테이너 ID, 정지 상태, restart=no를 확인합니다. 같은 데이터 볼륨에 차단 표식을 쓰고 시작 스크립트의 재시작 거부를 적용한 뒤 `pg_promote(false)`를 호출합니다. 별도 임시 디렉터리의 표식으로 재시작 거부도 사전에 검사했습니다. 승격 완료와 Timeline 증가를 확인합니다. 이 차단은 Compose 운영 규칙과 시작 스크립트에 의존하며 관리자가 이를 우회하는 상황이나 네트워크 분할을 다루지 않습니다.

새 쓰기와 재시도 전에 DB snapshot과 클라이언트 기록을 보존합니다. 승인된 ID가 새 Primary에 없는지를 이 시점에 판정하므로 재시도로 복구한 거래가 최초 유실을 가리지 않습니다. 아직 결과 기록이 없는 intent도 보수적으로 결과 불명확에 포함합니다.

새 Primary 쓰기를 확인한 뒤 작업자의 route.json을 원자적으로 바꿉니다. 새 Primary의 첫 앱 거래 성공을 관측하고 작업자를 정지합니다. 모든 intent를 같은 ID와 payload로 재시도합니다. DB와 무관한 Python 검증기가 초기 잔액과 요청 내용으로 기대 이력·원장·잔액을 계산해 DB 전체 행과 비교합니다. 실패하면 로그와 실패 결과를 보존하고 0이 아닌 종료 코드를 반환합니다.

## 이번 실행 결과

| 항목 | 실측 |
|---|---:|
| 장애 직전 승인 요청 | 159 |
| 재시도 전 승인 요청 생존 / 유실 | 159 / 0 |
| 재시도 전 결과 불명확 | 1 |
| 결과 불명확 중 새 Primary에 존재 | 0 |
| 전체 작업자 요청 시도 | 204 |
| 재시도 결과 new / existing | 3 / 201 |
| 최종 요청 / 원장 | 205 / 410 |
| 최종 잔액 | 999,795 / 1,000,205 |
| Timeline | 1 → 2 |
| 장애 감지 관측 | 0.812초 |
| 승격 요청부터 완료 확인 | 1.782초 |
| 승격 완료부터 라우팅 변경 | 3.703초 |
| 라우팅 변경부터 앱 성공 확인 | 5.234초 |
| 장애 요청부터 앱 성공 확인 | 13.828초 |
| 장애 전 승인 지연 p50 / p95 / p99 | 18.195 / 24.119 / 31.859ms |

최종 요청 수에는 별도 쓰기 확인 거래 1건이 포함됩니다. 재시도 new 3건은 승인 유실 3건을 뜻하지 않습니다. 승인되지 않은 실패·결과 불명확 요청 중 새 DB에 없던 요청을 다시 반영한 수입니다.

시간 구간은 모두 호스트의 monotonic 시계를 사용합니다. 컨테이너 시계와 호스트 시각을 빼지 않습니다. 13.828초는 Docker 호출, fsync, snapshot·쓰기 확인과 관측 대기까지 포함한 실험 제어기의 복구 관측 시간이며 DB 단독 승격 시간이나 운영 RTO 보장이 아닙니다. 지연은 작업자의 연결 시작부터 COMMIT 응답까지이며 결과 파일 fsync 시간은 제외합니다.

## 산출물과 현재 상태

[`metrics.json`](../experiments/stage4-failover/metrics.json), [`events.json`](../experiments/stage4-failover/events.json), [`fencing.json`](../experiments/stage4-failover/fencing.json), [`pre-retry-survival.json`](../experiments/stage4-failover/pre-retry-survival.json), [`client.jsonl`](../experiments/stage4-failover/client.jsonl), [`retries.jsonl`](../experiments/stage4-failover/retries.jsonl), [`final-verification.json`](../experiments/stage4-failover/final-verification.json)에 판정 근거를 보관합니다. 입력, DB snapshot, 요청 CSV, 실행 명령과 PostgreSQL 로그도 같은 폴더에 있습니다. 실행 원본은 results 아래에 보존합니다.

구 Primary는 종료 코드 137로 정지했고 restart=no와 차단 표식을 유지합니다. 서비스 이름 standby인 서버가 새 Primary입니다. 재편입을 마치기 전에는 구 Primary를 시작하거나 표식을 지우지 않습니다. 자동 장애 전환, 구 Primary 재편입, 동기 복제 비교, PITR은 이번 단계에 포함하지 않았습니다.
