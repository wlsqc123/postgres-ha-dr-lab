# Base Backup과 WAL로 삭제 직전 복구

## 결과

| 확인 항목 | 결과 |
|---|---|
| 백업 당시 | 요청 1건 |
| 목표 시점 복구 | **요청 3건·원장 6건**, 잔액 9,650 / 10,350 |
| 목표 이후 거래·삭제 | 반영되지 않음 |
| 복구 후 새 쓰기 | 성공, Timeline 1 → 2 |
| 필수 WAL 미제공 | 종료 코드 1, 목표 미도달 오류 |
| 복구 시작부터 목표 데이터 검증 | 4.829초 |

4.829초는 새 볼륨 복사·시작·목표 정지·데이터 조회까지의 호스트 관측 시간이다. 전체 서비스 복구 시간이나 운영 RTO를 보장하지 않는다.

## 어떻게 검증했는가

**백업 → 정상 거래 추가 → 목표 시각 기록 → 추가 거래·삭제 → WAL 보관 → 복구**

1. 별도 pitr-source에서 archive_mode=on과 archive_command를 설정했다. pg_basebackup -X stream으로 전체 백업을 받고 pg_verifybackup을 통과했다.
2. 백업에는 이체 1건만 담았다. WAL 파일을 전환하고 정상 이체 2건을 추가해 단순 백업 복원과 PITR을 구분했다.
3. DB의 clock_timestamp()를 복구 목표로 기록했다. 그 뒤 이체 1건을 더 넣고 실험 계좌·요청·원장을 삭제해 COMMIT을 확인했다.
4. pg_switch_wal()로 대상 WAL 보관을 완료했다. 대상 파일이 Base Backup의 pg_wal에 없다는 것도 검사했다.
5. 원본 서버 정지·restart=no·차단 표식을 확인한 뒤 별도 복구 볼륨을 시작했다. 다음 설정으로 목표 시점에 정지시켰다.

```conf
restore_command = 'bash /etc/lab/restore_wal.sh %f %p'
recovery_target_time = '2026-10-18 13:47:06.026214+00:00'
recovery_target_inclusive = on
recovery_target_timeline = '1'
recovery_target_action = 'pause'
```

recovery.signal로 아카이브 복구를 시작했다. 목표는 DB가 기록한 UTC 시각이며 클라이언트 시계로 추정하지 않았다. recovery=true와 pause_state=paused를 확인한 뒤 초기 잔액·요청 목록으로 계산한 이력·원장·잔액과 대조했다. 백업 이후 정상 거래는 있고 목표 이후 거래는 없어야 통과한다.

검증 후 pg_wal_replay_resume()으로 복구를 마치고 Timeline 2와 새 쓰기를 확인했다. 새 쓰기 확인 거래를 포함한 최종 결과는 요청 4건·원장 8건이다. [PostgreSQL 18 PITR](https://www.postgresql.org/docs/18/continuous-archiving.html)

## WAL이 없으면 어떻게 되는가

필수 WAL `000000010000000000000004`를 별도 복구 인스턴스의 restore_command에서만 제공하지 않았다. 실제 보관 파일은 삭제하지 않았다.

복구는 `recovery ended before configured recovery target was reached` 오류와 종료 코드 1로 실패했다. 단순 연결 실패를 성공적인 방어로 취급하지 않고, 필수 파일 미제공 기록·목표 미도달 오류·비정상 종료를 모두 검사했다.

archive_command는 임시 파일을 디스크에 반영한 뒤 덮어쓰기 없는 hard link로 공개한다. 같은 이름이 이미 있으면 내용이 같은 경우만 성공 처리한다. 복구 인스턴스는 백업·WAL 볼륨을 읽기 전용으로 사용한다.

## 재현과 기록

```powershell
python -m ledger.pitr --confirm-isolated-delete
```

5단계 환경과 runner가 실행 중이고 stage7 리소스가 없는 상태에서 한 번 실행한다. 기존 데이터와 분리된 실험 계좌만 삭제한다. 완료 후 PITR용 DB만 정지하며 백업·WAL·삭제된 원본·복구 볼륨을 모두 보존한다. 기존 리소스가 있으면 덮어쓰거나 삭제하지 않고 중단한다.

테스트 37개와 기존 재편입 환경의 역할·시스템 식별자·계좌 범위 데이터 보존을 확인했다. 삭제 함수도 pitr-source의 지정 계좌 두 개만 허용한다. 백업과 WAL은 같은 PC의 별도 볼륨에 있으므로 호스트 전체 유실 대응을 검증한 것은 아니다.

[제어 코드](../ledger/pitr.py) · [구성](../compose.pitr.yaml) · [실측 결과](../experiments/stage7-pitr.json) · [실행 기록](../experiments/stage7-pitr/)

실행 ID: `20261018T134638Z-35ea6f2d`. 공개 기록은 입력·snapshot·백업 검증·WAL 해시·복구 로그이며, 인증 정보가 포함된 실제 Base Backup과 WAL 원본은 GitHub에 올리지 않는다.
