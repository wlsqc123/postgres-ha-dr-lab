# 동기·비동기 복제 비교

단일 PC의 별도 DB 두 쌍으로 비교했다. 기존 재편입 환경은 유지했다.

## Standby 중단과 장애 전환

| 확인 항목 | 비동기 | 동기 |
|---|---|---|
| Standby 중단 중 이체 | 9.318ms에 승인 | SyncRep 대기, 재시작 후 승인 |
| 승격 직후 승인 거래 유실 | 10 / 321건 | 0 / 311건 |
| 장애 당시 결과 불명확 | 0건 | 1건 |
| 재시도 후 최종 요청 / 원장 | 321 / 642건 | 312 / 624건 |

비동기에서 사라진 10건은 Standby를 중단한 뒤 보낸 요청이다. 동기는 그 구간의 첫 요청부터 승인을 기다렸다. 동기 대기 요청은 Standby 재시작까지 포함해 7,088.691ms에 완료됐다. Primary 장애 실험은 이와 별도로 다시 Standby를 중단한 뒤 수행했다.

## 정상 상태

| 이체 승인 지연 | 비동기 | 동기 |
|---|---:|---:|
| p50 | 6.398ms | 10.627ms |
| p95 | 21.545ms | 28.664ms |
| p99 | 55.871ms | 50.810ms |

각 방식은 워밍업 10건을 제외하고 100건씩 3회 측정했다. 실행 순서는 async→sync, sync→async, async→sync였다. 연결 수립과 기록 파일 fsync는 지연 측정에서 제외하고 이체 트랜잭션 시작부터 COMMIT 응답까지 측정했다. 원본 300개 표본의 정렬 위치 floor((n−1)×q)를 사용했다. 같은 PC의 작은 표본이며 모든 분위수에서 동기가 느리지는 않았다.

## 설정과 장애 조건

| 설정 | 비동기 | 동기 |
|---|---|---|
| synchronous_commit | on | on |
| synchronous_standby_names | 빈 값 | FIRST 1 (standby_stage2) |
| pg_stat_replication.sync_state | async | sync |

동기의 on은 지정 Standby의 WAL flush 확인을 기다린다. WAL replay 완료를 기다리는 remote_apply 설정은 사용하지 않았다. [PostgreSQL 18 동기 복제](https://www.postgresql.org/docs/18/warm-standby.html#SYNCHRONOUS-REPLICATION)

1. **Standby 중단:** docker compose stop으로 WAL 수신을 끊은 뒤 이체 1건을 보냈다. 동기는 실제 SyncRep 대기와 2초 관측 구간 동안 미완료 상태를 확인하고 Standby를 다시 시작했다.
2. **복제 단절 중 Primary 장애:** 다시 복제가 따라잡은 것을 확인한 뒤 Standby를 중단했다. 비동기는 10건을 승인했고 동기는 첫 요청이 SyncRep에서 대기했다. Primary를 SIGKILL로 종료하고 정지·restart=no·차단 표식을 확인한 뒤 Standby를 시작하고 승격했다.
3. **재시도 전 판정:** 클라이언트 승인 ID와 승격 직후 snapshot을 비교했다. 이후에만 같은 ID로 재시도하고 독립적인 기대 이력·원장·잔액을 검증했다.

이 조건은 복제 단절 구간을 의도적으로 만든 실험이다. 일반적인 Primary 단독 장애의 유실률이나 운영 성능으로 해석하지 않는다. 동기의 미승인 요청도 롤백으로 단정하지 않고 결과 불명확으로 기록했다.

## 재현과 기록

5단계 환경과 runner가 실행 중이고 비교용 컨테이너·볼륨이 없는 상태에서 한 번 실행한다.

```powershell
python -m ledger.compare --confirm-comparison-faults
```

같은 Compose 프로젝트에 stage6 전용 서비스 네 개와 볼륨 네 개를 추가한다. 완료 후 비교용 DB만 정지하고 모든 볼륨과 실패 기록을 보존한다. 종료된 비교용 Primary는 재시작하지 않는다. 이전 비교 리소스가 있으면 재사용하거나 삭제하지 않고 중단한다.

재시도 후 두 환경의 이력·원장·잔액은 독립적인 기대값과 일치했다. 테스트 28개를 통과했다. 기존 재편입 환경은 실험 전후 동일한 계좌 범위의 snapshot과 역할·시스템 식별자를 비교해 보존을 확인했다.

[제어 코드](../ledger/compare.py) · [측정 코드](../ledger/compare_worker.py) · [구성](../compose.compare.yaml) · [실측 결과](../experiments/stage6-compare.json)

[원본 기록](../experiments/stage6-compare/)에는 각 300건의 지연 표본, 클라이언트 승인 기록, SyncRep 대기, 재시도 전 생존 판정과 DB snapshot을 보관한다. 실행 ID는 `20261018T132454Z-70feae5d`다.
