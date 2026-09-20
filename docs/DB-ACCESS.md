# DBeaver 운영 DB 접속

## 현재 접속 상태와 먼저 확인할 것

사용자는 서버 DB 포트를 15432로 변경하고 기존 비밀번호 SSH 터널을 사용하는 안내 후 DBeaver 접속 성공을 확인했습니다. 다만 실제 SSH 계정·적용된 Match 규칙·조회 전용 DB 계정 사용 여부는 직접 검증하지 않았습니다. 저장소 `compose.prod.yaml`은 여전히 `127.0.0.1:5432:5432`입니다. 이 문서 변경만으로 서버나 Compose 설정을 변경하지 않습니다.

운영 서버에서 먼저 읽기 전용 확인을 합니다.

```bash
cd ~/app
git status --short
git diff -- compose.prod.yaml
docker compose -f compose.prod.yaml port postgres 5432
```

현재 접속을 유지하려면 출력 포트를 DBeaver **Main**의 `127.0.0.1` Port에 사용하고 **SSH**에는 실제 서버 주소·SSH 포트·기존에 성공한 인증을 유지합니다. 봇의 DB 주소는 `postgres:5432`입니다. 포트 출력에 외부 주소 `0.0.0.0` 또는 `[::]`가 보이면 접속을 확대하지 말고 노출 설정부터 점검합니다. 방화벽에 DB 포트를 열지 않습니다.

서버 Compose에 15432 변경이 남아 있다면 재배포 전에 차이를 검토합니다. 무조건 덮어쓰거나 5432로 바꾸지 않습니다. SSH의 `PermitOpen` 목적지와 실제 바인딩·DBeaver Main 포트가 같아야 합니다. 관리자는 기존 세션을 유지한 상태에서 아래 1단계의 정책 확인 및 `sshd -T -C user=<실제계정>,host=client,addr=<접속PC의IP>`로 적용값을 확인합니다. 비밀번호 로그인 자체가 조회 전용 DB 권한을 보장하지 않으므로 아래 6단계의 계정 검증은 별도로 필요합니다.

## 선택 사항: 전용 키 인증 터널 계정으로 분리

아래는 이전에 작성한 **5432 기준 전용 계정 구성안**이며 현재 서버에 이미 적용됐다는 뜻이 아닙니다. 현재 비밀번호 접속을 유지할지, 전용 계정으로 분리할지 결정한 뒤 필요한 단계만 적용하세요. 15432를 유지하면서 이 구성을 선택하면 아래 `permitopen`, `PermitOpen`, 바인딩 확인·DBeaver 포트를 모두 15432로 일치시킵니다. 컨테이너 내부 포트 5432는 변경하지 않습니다. 기존 배포 계정 정책도 실제 효과를 확인하고 별도 결정 없이 덮어쓰지 않습니다.

## 1. 먼저 관리자 계정에서 확인

현재 관리자 SSH 세션은 끝까지 열어 두세요. 비밀키·비밀번호는 공유하지 않습니다. 서버의 실제 SSH 정책을 확인하기 전 설정을 덮어쓰거나 ssh 서비스를 재시작하지 않습니다.

```bash
sudo -n /usr/sbin/sshd -t
sudo -n /usr/sbin/sshd -T | grep -E '^(port|allowusers|allowgroups|denyusers|denygroups|disableforwarding|authorizedkeysfile) '
sudo -n grep -R -nE '^[[:space:]]*(Include|Match|AllowUsers|AllowGroups|DenyUsers|DenyGroups|DisableForwarding|AllowTcpForwarding|PermitOpen|AuthenticationMethods)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d
```

AllowUsers/AllowGroups 제한이 있으면 전용 계정 허용 여부를 먼저 검토합니다. 포워딩을 금지하는 `seulseul-deploy` 그룹에 전용 계정을 넣어 해결하지 마세요. 중첩 Match·Include 우선순위를 확인한 후 아래 설정을 적용합니다. `sshd -T -C`에서 기대값이 아니면 진행하지 않습니다.

## 2. 내 PC에서 전용 키 생성

```bash
ssh-keygen -t ed25519 -f ~/.ssh/seulseul_db_tunnel -C seulseul-db-tunnel
```

기존 파일이 있으면 덮어쓰지 마세요. 키 암호를 설정하고 `.pub` 공개키만 서버에 등록합니다. 확장자 없는 파일은 비밀키로, 서버나 저장소에 업로드하지 않습니다.

## 3. 관리자 계정에서 전용 계정·키 등록

아래 계정이 이미 있다면 생성 명령을 재실행하지 말고 용도를 먼저 확인합니다.

```bash
sudo -n adduser --disabled-password --gecos '' --shell /usr/sbin/nologin seulseul-tunnel
sudo -n install -d -m 700 -o seulseul-tunnel -g seulseul-tunnel /home/seulseul-tunnel/.ssh
sudo -n touch /home/seulseul-tunnel/.ssh/authorized_keys
sudo -n chown seulseul-tunnel:seulseul-tunnel /home/seulseul-tunnel/.ssh/authorized_keys
sudo -n chmod 600 /home/seulseul-tunnel/.ssh/authorized_keys
sudo -n nano /home/seulseul-tunnel/.ssh/authorized_keys
```

자신의 공개키를 다음 옵션과 함께 **한 줄로** 추가합니다. 아래 예시를 그대로 키로 사용하지 않습니다.

```text
restrict,port-forwarding,permitopen="127.0.0.1:5432" ssh-ed25519 실제공개키 seulseul-db-tunnel
```

전용 계정은 sudo·docker·seulseul-deploy 그룹에 추가하지 않습니다. 키 로그인 실패가 반복되면 fail2ban에 차단될 수 있으므로 반복 시도 대신 관리자 로그를 확인합니다.

## 4. 관리자 계정에서 목적지 한정 SSH 설정

1단계 정책 확인 후 전용 파일을 편집합니다. 기존 파일이 있으면 먼저 내용을 확인합니다.

```bash
sudo -n nano /etc/ssh/sshd_config.d/00-seulseul-tunnel.conf
```

```text
Match User seulseul-tunnel
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    DisableForwarding no
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:5432
    PermitListen none
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
    MaxSessions 0
Match all
```

쉘·SFTP 세션은 막고 TCP 터널만 허용합니다. 다음 명령의 `클라이언트IP`는 실제 접속 PC 공인 IP로 바꿉니다.

```bash
sudo -n /usr/sbin/sshd -t
sudo -n /usr/sbin/sshd -T -C user=seulseul-tunnel,host=client,addr=클라이언트IP | grep -E '^(authenticationmethods|passwordauthentication|kbdinteractiveauthentication|disableforwarding|allowtcpforwarding|permitopen|permitlisten|maxsessions|permittty) '
sudo -n /usr/sbin/sshd -T -C user=seulseul,host=client,addr=클라이언트IP | grep -E '^(passwordauthentication|allowtcpforwarding|disableforwarding) '
```

전용 계정은 publickey만, local 포워딩, 목적지 127.0.0.1:5432, MaxSessions 0이어야 합니다. 기존 배포 계정은 포워딩 금지가 유지돼야 합니다. 문법·효과 검증 후에만 reload합니다.

```bash
sudo -n systemctl reload ssh.service
```

관리자 연결을 유지한 채 별도 창에서 관리자 로그인이 여전히 되는지 확인합니다. 실패 시 열려 있는 세션에서 전용 설정 파일을 Include 대상 밖으로 옮기고 `sshd -t` 후 reload하여 되돌립니다. 기존 SSH 정책은 삭제하지 않습니다.

## 5. seulseul 계정에서 DB 포트 적용

저장소 변경을 커밋·푸시한 뒤 서버에서 받습니다. 기존 운영 DB를 먼저 백업하세요. 5432가 이미 점유 중이면 다른 프로세스를 종료하지 말고 진행을 멈춥니다.

```bash
cd ~/app
git status --short
git pull --ff-only origin main
ss -ltn '( sport = :5432 )'
./run.sh
docker compose -f compose.prod.yaml port postgres 5432
ss -ltn '( sport = :5432 )'
```

Docker가 꺼져 있다면 `systemctl --user start docker.service` 후 실행합니다. 표시 주소는 `127.0.0.1:5432`여야 합니다. `0.0.0.0:5432` 또는 `[::]:5432`이면 즉시 봇·DB를 중지하고 설정을 확인합니다. Oracle 보안 목록이나 호스트 방화벽에 5432 허용 규칙을 추가하지 않습니다. 봇의 DATABASE_URL은 기존 `postgres:5432` 그대로입니다. 다른 서버 로컬 계정도 루프백 포트에 접근할 수 있으므로 DB 인증은 필수입니다.

## 6. 조회 전용 DB 계정

`view.sh db`는 기본 읽기 전용이므로 계정 생성에는 관리자 psql을 별도로 엽니다.

```bash
docker compose -f compose.prod.yaml exec postgres sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

아래는 `seulseul_reader`가 아직 없을 때 한 번 실행합니다. 비밀번호는 `\password`의 숨김 프롬프트에 입력하고 SQL·셸 명령에 직접 쓰지 않습니다.

```sql
CREATE ROLE seulseul_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\password seulseul_reader
SELECT format('GRANT CONNECT ON DATABASE %I TO seulseul_reader', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO seulseul_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO seulseul_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO seulseul_reader;
ALTER ROLE seulseul_reader SET default_transaction_read_only = on;
\q
```

기본 권한은 이 명령을 실행한 객체 생성자에게만 적용됩니다. 현재 마이그레이션 사용자와 동일한 POSTGRES_USER로 실행하고, 향후 생성자가 달라지면 기본 권한도 재검토합니다. 학생 개인정보를 읽을 수 있는 계정이므로 신뢰하는 운영자에게만 제공합니다. 앱의 운영 DB 비밀번호를 DBeaver 용도로 공유하지 않습니다.

## 7. DBeaver 연결

PostgreSQL 연결을 만들고 아래 값을 입력합니다. 포트가 다른 SSH 서버라면 실제 SSH 포트를 사용합니다.

| 탭 | 항목 | 값 |
| --- | --- | --- |
| Main | Host / Port | `127.0.0.1` / `5432` |
| Main | Database | 운영 POSTGRES_DB 값 |
| Main | User / Password | `seulseul_reader` / 6단계에서 정한 비밀번호 |
| SSH | Use SSH Tunnel | 활성화 |
| SSH | Host / Port | 서버 주소 / 기존 SSH 포트 |
| SSH | User | `seulseul-tunnel` |
| SSH | Authentication | Public Key |
| SSH | Private key | 내 PC의 `~/.ssh/seulseul_db_tunnel` (확장자 없는 파일) |

호스트 키는 관리자에게 확인한 서버 지문과 대조하세요. DBeaver 자동 로컬 포트 할당을 사용하면 PC의 PostgreSQL과 포트 충돌을 피할 수 있습니다. 연결 후 `SELECT current_user, current_database();`와 `SHOW default_transaction_read_only;`로 계정·DB·읽기 전용 기본값을 확인합니다. 쉘 세션을 요구하는 SSH 테스트가 실패하면 제한을 풀지 말고 DB 연결 테스트나 `ssh -N` 방식으로 확인합니다.

실패 시 인증 실패 / forwarding denied / DB connection refused / DB 인증 실패를 구분해 확인합니다. 로그·스크린샷에는 학생 데이터·키·비밀번호를 포함하지 마세요.

참고: [Docker 포트 바인딩](https://docs.docker.com/engine/network/port-publishing/), [OpenSSH 설정](https://man.openbsd.org/sshd_config), [DBeaver SSH](https://dbeaver.com/docs/dbeaver/SSH-Configuration/).
