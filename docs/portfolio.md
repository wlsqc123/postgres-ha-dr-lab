# PostgreSQL 장애 전환과 복구

Primary를 강제 종료하고 **거래 유실·중복 여부와 쓰기 복구 시간**을 검증했다.

`PostgreSQL 18` · `Docker` · `Python` · `동기·비동기 물리 복제`

## 첫 장애 전환 결과

| 승인 거래 유실 | 앱 쓰기 복구 관측 | 재편입 후 데이터 |
|:---:|:---:|:---:|
| **0 / 159건** | **13.828초** | **요청 206건·원장 412건 일치** |

단일 PC의 수동 장애 전환 실험이다. 복구 시간은 제어·검증 시간을 포함하며 비동기 복제의 무손실을 보장하지 않는다.

## 어떻게 복구했는가

**강제 종료 → 기존 Primary 차단 → Standby 승격 → 앱 연결 전환 → 재편입**

### 1. 거래 부하 중 Primary 강제 종료

```powershell
docker compose kill -s SIGKILL primary
```

**확인:** 종료 코드 `137` + 실제 클라이언트 연결 실패.

### 2. 기존 Primary 차단 후 Standby 승격

`Running=false`, `restart=no`를 검사하고 `lab.fenced` 표식으로 재시작을 막았다.

```sql
SELECT pg_promote(false);     -- 승격 요청
SELECT pg_is_in_recovery();   -- false로 바뀔 때까지 확인
```

**확인:** recovery `true → false`, Timeline `1 → 2`. 승격 완료 관측 **1.782초**.

### 3. 유실 측정 후 앱 연결 전환·재시도

| 순서 | 처리 | 확인 |
|---|---|---|
| 재시도 전 | 클라이언트 승인 ID와 승격 직후 DB 비교 | 승인 159건 모두 존재 |
| 연결 전환 | 새 Primary 쓰기 확인 후 `route.json`의 호스트 변경 | 앱의 새 거래 성공 |
| 재시도 후 | 같은 요청 ID·내용으로 재전송 | 요청 205건·원장 410건 일치 |

**판정 기준:** 초기 잔액과 요청 목록으로 기대값을 계산해 실제 이력·원장·잔액과 비교했다. 재시도 전에 유실을 측정해 사라진 거래가 재생성돼도 최초 유실이 가려지지 않게 했다.

### 4. 새 백업으로 기존 Primary 재편입

| 방법 | 확인 |
|---|---|
| 장애 디스크 보존 + 별도 볼륨에 `pg_basebackup -R -X stream` | 기존 데이터 덮어쓰기 없음 |
| `standby.signal`·새 Primary 연결·복제 slot 설정 | recovery=true, streaming |
| 새 거래와 동일 ID 재시도 | 양쪽 요청 206건·원장 412건 일치 |
| Standby 쓰기 시도·재시작 | 쓰기 거부 `25006`, 복제 유지 |

실습 데이터 규모에서 기존 디스크를 보존하며 전체 복사본으로 재구성하려고 새 백업을 선택했다. `pg_rewind`와 원래 Primary로 역할을 되돌리는 failback은 수행하지 않았다.

## 동기·비동기 비교

| 같은 조건의 비교 | 비동기 | 동기 |
|---|---|---|
| 정상 이체 승인 지연 p95 · 각 300건 | 21.545ms | 28.664ms |
| Standby 중단 | 승인 계속 | SyncRep에서 승인 대기 |
| 중단 상태에서 Primary 종료 후 승인 유실 | 10 / 321건 | 0 / 311건 |

복제 단절을 의도적으로 만든 실험이다. 동기는 대기 중이던 1건을 결과 불명확으로 기록했다. [측정 조건과 전체 결과](stage6-replication-compare.md)

## 코드와 실행 기록

| 단계 | 코드 | 실측 기록 |
|---|---|---|
| 장애 전환 | [제어 스크립트](../ledger/failover.py) | [결과](../experiments/stage4-failover.json) · [재시도 전 유실 판정](../experiments/stage4-failover/pre-retry-survival.json) |
| 재편입 | [제어 스크립트](../ledger/rejoin.py) | [결과](../experiments/stage5-rejoin.json) |
| 동기·비동기 비교 | [제어 스크립트](../ledger/compare.py) | [결과](../experiments/stage6-compare.json) |
| PITR | [제어 스크립트](../ledger/pitr.py) | [결과](../experiments/stage7-pitr.json) |

## 삭제 직전 시점 복구 — PITR

**Base Backup → 정상 거래 추가 → 목표 시각 기록 → 삭제 → WAL 재생**

| 검증 | 결과 |
|---|---|
| 백업 이후 정상 거래까지 복구 | 요청 3건·원장 6건 일치 |
| 목표 이후 거래·삭제 제외 | 확인 |
| 필수 WAL 미제공 | 목표 미도달 오류로 복구 실패 |

`recovery_target_time`과 `recovery.signal`로 복구하고, 목표 시점에서 정지해 데이터를 검증했다. [설정과 실행 기록](stage7-pitr.md)

자동 장애 전환·네트워크 분할은 아직 검증하지 않았다. 상세 절차는 [장애 전환](stage4-failover.md)과 [재편입](stage5-rejoin.md)에 분리했다.

[면접 설명 요약](interview.md) · [최종 점검 기록](../experiments/final-verification.json)
