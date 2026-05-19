목표



Gemness의 Gemini 실행 구조를 “1회 실행 기록 더미”에서 “Observer 대화방 단위의 지속 대화”로 변경한다. Gemini CLI subprocess는 계속 1회 실행 방식으로 유지하되, Gemini CLI native session 기능인 --session-id와 --resume을 사용해 대화 context를 이어간다. Observer 사용자는 하나의 conversation\_id 안에서 Codex/Gemini가 맥락을 이어가는 것처럼 볼 수 있어야 한다.



핵심 설계



새 식별자를 도입한다.



conversation\_id:

&#x20; 사용자-facing 대화방 ID.

&#x20; Observer에서 하나의 대화 timeline을 구성하는 단위.



run\_id:

&#x20; Gemini CLI subprocess 실행 1회 ID.

&#x20; 기존 session\_id의 실질 역할.

&#x20; stream-json event, final result, error, duration, command argv를 기록하는 단위.



gemini\_session\_id:

&#x20; Gemini CLI native session ID.

&#x20; conversation\_id와 매핑된다.

&#x20; 사용자에게 노출하지 않는다.



기존 session\_id는 하위 호환을 위해 당분간 유지하되, 내부적으로는 run\_id로 취급한다.



데이터 모델



conversation transcript/index에 다음 필드를 추가한다.



{

&#x20; "conversation\_id": "conv\_...",

&#x20; "title": "...",

&#x20; "created\_at": "...",

&#x20; "updated\_at": "...",

&#x20; "project\_root": "...",

&#x20; "model": "...",

&#x20; "approval\_mode": "plan",

&#x20; "current\_gemini\_session\_id": "gemness\_...",

&#x20; "native\_resume\_enabled": true,

&#x20; "fallback\_mode": "none",

&#x20; "summary": null,

&#x20; "turn\_count": 0

}



각 run에는 다음 필드를 둔다.



{

&#x20; "run\_id": "run\_...",

&#x20; "conversation\_id": "conv\_...",

&#x20; "parent\_run\_id": null,

&#x20; "branch\_from\_run\_id": null,

&#x20; "turn\_index": 1,

&#x20; "prompt": "...",

&#x20; "status": "running|succeeded|failed",

&#x20; "gemini\_session\_id": "gemness\_...",

&#x20; "native\_resume\_used": true,

&#x20; "fallback\_used": false,

&#x20; "fallback\_reason": null,

&#x20; "command\_argv": \[],

&#x20; "stream\_events\_path": "...",

&#x20; "final\_result": "...",

&#x20; "error": null,

&#x20; "created\_at": "...",

&#x20; "finished\_at": "..."

}

Gemini session ID 생성 규칙



첫 턴에서 새 gemini\_session\_id를 생성한다.



gemini\_session\_id = "gemness\_" + uuid\_without\_braces



허용 문자는 영숫자, 대시, 언더스코어만 사용한다.



conversation\_id와 gemini\_session\_id는 반드시 transcript/index에 먼저 저장한 뒤 subprocess를 실행한다. 프로세스가 중간에 죽어도 mapping을 복구할 수 있어야 한다.



Runner 명령 생성



첫 턴:



argv = \[

&#x20;   "gemini",

&#x20;   "-m", model,

&#x20;   "--output-format", "stream-json",

&#x20;   "--approval-mode", "plan",

&#x20;   "--session-id", gemini\_session\_id,

&#x20;   "-p", prompt,

]



후속 턴:



argv = \[

&#x20;   "gemini",

&#x20;   "-m", model,

&#x20;   "--output-format", "stream-json",

&#x20;   "--approval-mode", "plan",

&#x20;   "--resume", gemini\_session\_id,

&#x20;   "-p", prompt,

]



절대 shell string으로 조립하지 말고 argv list로 subprocess를 실행한다. Windows quoting, 줄바꿈, 따옴표, prompt injection성 shell 문자를 안전하게 처리하기 위함이다.



Capability probe



Gemness 시작 시 또는 첫 Gemini 호출 전 다음을 확인한다.



gemini --version

gemini --help



확인할 항목:



\--resume 존재

\--session-id 존재

\--list-sessions 존재

\--output-format 존재

stream-json 지원

\-p/--prompt 지원



지원되면 native mode를 켠다.



지원되지 않으면 기존 prompt-injection follow-up 방식으로 fallback한다.



환경 변수 또는 설정값을 둔다.



GEMNESS\_GEMINI\_NATIVE\_RESUME=auto|on|off



기본값은 auto.



auto:

&#x20; capability probe 성공 시 native resume 사용.

&#x20; 실패 시 fallback.



on:

&#x20; native resume 필수.

&#x20; 실패 시 명시적 에러.



off:

&#x20; 기존 방식 사용.

Follow-up 처리



최신 턴에 대한 follow-up이면 native resume을 사용한다.



1\. conversation\_id 조회

2\. conversation lock 획득

3\. current\_gemini\_session\_id 조회

4\. 새 run\_id 생성

5\. gemini --resume <gemini\_session\_id> -p <prompt> 실행

6\. stream-json event를 run\_id에 기록

7\. final result 저장

8\. conversation updated\_at, turn\_count 갱신

9\. lock 해제



ObserverHub.build\_follow\_up\_prompt()는 native 경로에서 호출하지 않는다. fallback 전용으로만 사용한다.



Branch 처리



최신 턴이 아닌 과거 run에서 follow-up을 누르면 같은 gemini\_session\_id를 resume하지 않는다.



대신 새 conversation branch를 만든다.



1\. 새 conversation\_id 생성

2\. 새 gemini\_session\_id 생성

3\. branch\_from\_conversation\_id 저장

4\. branch\_from\_run\_id 저장

5\. 원본 conversation의 해당 run까지의 prompt/result를 기반으로 fallback context 생성

6\. 새 Gemini native session을 --session-id로 시작

7\. 이후 branch conversation은 native resume 사용



Observer UI에서는 “새 분기 대화”로 보여준다. 기존 대화 timeline을 오염시키지 않는다.



Fallback 정책



다음 경우 fallback을 사용한다.



\- capability probe 실패

\- --resume 실행 실패

\- Gemini session file이 삭제/만료됨

\- invalid session identifier

\- maxSessionTurns 또는 context/session 관련 에러

\- 과거 턴에서 branch 생성

\- 사용자가 native resume off 설정



fallback prompt 구성:



You are continuing a previous Gemini advisory conversation inside Gemness.



Conversation summary:

<summary if available>



Recent turns:

User: ...

Gemini: ...

User: ...

Gemini: ...



New user request:

<current prompt>



fallback 사용 시에도 가능하면 새 gemini\_session\_id로 native session을 시작한다.



gemini --session-id <new\_gemini\_session\_id> -p "<fallback\_prompt>"



이후 해당 conversation 또는 branch는 새 native session으로 이어간다.



절대 --resume latest를 자동으로 쓰지 않는다. 잘못된 프로젝트/다른 대화로 붙을 위험이 있다.



Long session rotation



긴 대화가 누적되면 native session을 계속 이어가지 말고 회전한다.



조건 예시:



turn\_count >= 40

또는 transcript token estimate >= configured threshold

또는 resume latency가 비정상적으로 길어짐

또는 Gemini CLI session/context 관련 에러 발생



회전 방식:



1\. Gemness transcript 기준 conversation summary 생성

2\. 새 gemini\_session\_id 생성

3\. 같은 conversation\_id에 current\_gemini\_session\_id 갱신

4\. 첫 prompt에 summary + 최근 N턴 + 현재 요청을 포함해 --session-id로 시작

5\. run에는 fallback\_used=true, fallback\_reason="session\_rotation" 기록



사용자 UI에는 별도 경고를 크게 띄우지 않는다. 필요하면 debug panel에만 표시한다.



Observer UI 변경



Observer는 conversation\_id 기준 timeline을 보여준다.



각 turn은 내부적으로 run\_id를 가진다.



Conversation

&#x20; Turn 1 / run\_id

&#x20;   prompt

&#x20;   Gemini stream

&#x20;   final result

&#x20; Turn 2 / run\_id

&#x20;   follow-up prompt

&#x20;   Gemini stream

&#x20;   final result



기존 /session/<session\_id> URL은 깨지지 않게 한다.



/session/<old\_session\_id>

→ 해당 run\_id를 찾음

→ /conversation/<conversation\_id>#run-<run\_id> 로 redirect 또는 내부 렌더링



사용자에게 gemini\_session\_id는 노출하지 않는다. debug view에서는 표시 가능하다.



Project root 고정



Gemini CLI session은 프로젝트별로 관리되므로, conversation 생성 시 project\_root를 저장하고 이후 모든 follow-up은 같은 cwd에서 실행한다.



subprocess.Popen(argv, cwd=conversation.project\_root, ...)



다른 cwd에서 resume하지 않는다.



프로젝트 root가 사라졌거나 접근 불가하면 native resume을 시도하지 말고 fallback 또는 에러를 낸다.



테스트



최소 acceptance test는 다음과 같다.



GID="gemness\_test\_$(uuidgen | tr '\[:upper:]' '\[:lower:]' | tr -d '{}')"



gemini \\

&#x20; --output-format stream-json \\

&#x20; --session-id "$GID" \\

&#x20; -p "Remember this exact token: GEMNESS\_NATIVE\_RESUME\_SMOKE\_7319. Reply only OK."



gemini \\

&#x20; --output-format stream-json \\

&#x20; --resume "$GID" \\

&#x20; -p "What exact token did I ask you to remember? Reply only the token."



기대 결과:



GEMNESS\_NATIVE\_RESUME\_SMOKE\_7319



추가 테스트:



\- --approval-mode plan 포함 smoke test

\- -m <model> 포함 smoke test

\- 같은 conversation에 follow-up 2개 동시 요청 시 순차 실행되는지

\- 서로 다른 conversation은 병렬 실행되는지

\- cwd가 바뀌면 resume 실패/불일치가 감지되는지

\- invalid gemini\_session\_id일 때 fallback 되는지

\- 중간 턴 follow-up이 branch conversation으로 생성되는지

\- 기존 session\_id URL이 새 conversation/run 구조로 호환되는지

\- Windows에서 prompt quoting이 깨지지 않는지

\- stream-json parser가 기존 Observer event 처리와 호환되는지

명시적으로 하지 말 것

\- Gemini interactive process를 장시간 유지하지 말 것.

\- --prompt-interactive를 Observer streaming 기본 경로로 쓰지 말 것.

\- --resume latest를 자동 fallback으로 쓰지 말 것.

\- conversation\_id와 run\_id를 같은 개념으로 유지하지 말 것.

\- native resume 실패 시 사용자 몰래 엉뚱한 Gemini session으로 붙이지 말 것.

\- 과거 턴 branch를 같은 gemini\_session\_id resume으로 처리하지 말 것.

