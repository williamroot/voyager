# Nó GPU macOS (Apple Silicon) — serving do extrator

> Mac mini M4 como nó de GPU pra **extração com LLM** (`extrator-precatorio-sdk`),
> servindo os GGUF via `llama.cpp` com **Metal**. Não é worker RQ de enrichment —
> não roda Django, não toca Postgres/Redis de prod.

Documento irmão: [`TREINAMENTOS.md`](TREINAMENTOS.md) §5 (onde as GPUs rodam),
[`EXTRACAO_ROADMAP.md`](EXTRACAO_ROADMAP.md) §P1 (pool multi-GPU).

## A máquina

| Item | Valor |
|---|---|
| Host | `Mac-mini-de-Davi.local` |
| Chip | **Apple M4**, 10 cores GPU (`MTLGPUFamilyApple9`, Metal 4) |
| RAM | **24 GB unificados** (working set Metal: 21,4 GB) |
| Disco | 460 GB (432 GB livres) |
| SO | macOS **26.5.2** (build 25F84), arm64 |
| Usuário | `davicordeiro` |
| Tailscale | **`voyager-worker-mac` = `100.105.16.107`** (daemon do brew, `utun0`) |

### Duas interfaces — atenção ao endereço que você usa

| Interface | Rede | IP | Config |
|---|---|---|---|
| `en0` **Ethernet** (cabo) | `192.168.1.x` | `192.168.1.13` | DHCP · **rota default** (gw `192.168.1.1`) |
| `en1` **Wi-Fi** | `192.168.200.x` | **`192.168.200.37`** | **estático** (gw `.200.1`, DNS 1.1.1.1/8.8.8.8) |

O Wi-Fi era DHCP e trocou de IP sozinho num reboot (`.37` → `.24`), quebrando todo
acesso salvo. Fixado em 2026-08-07:

```bash
sudo networksetup -setmanual "Wi-Fi" 192.168.200.37 255.255.255.0 192.168.200.1
sudo networksetup -setdnsservers "Wi-Fi" 1.1.1.1 8.8.8.8
```

**Nenhuma das duas alcança prod.** `192.168.30.x` não tem rota por nenhuma
interface — `.30.101:6432` falha do cabo e do Wi-Fi. Todo tráfego com o resto do
Voyager passa por **Tailscale**; o llmsv2 responde via DERP(sao) em ~13ms.

```
Mac mini (192.168.200.37 · ilha)
   └── Tailscale 100.105.16.107
         ├── llmsv2   100.122.213.79   (GGUF + repo bare do SDK)
         ├── voyager  100.100.144.57   (web/showcase)
         └── zordon   100.116.189.18
```

## Desempenho medido (o número que decide)

`llama-bench` no **Qwen2.5-7B-Instruct Q4_K_M** — mesma arquitetura e quantização
do extrator v2.1:

```
| qwen2 7B Q4_K - Medium | 4.36 GiB | 7.62 B | BLAS,MTL | pp512 | 244,67 ± 0,41 t/s |
| qwen2 7B Q4_K - Medium | 4.36 GiB | 7.62 B | BLAS,MTL | tg128 |  22,86 ± 0,26 t/s |
```

Traduzindo pra janela do SDK (`MAX_CHARS_DOC=9000` ≈ ~3k tokens de prompt,
ficha JSON ≈ ~300 tokens de saída):

```
prompt   3.000 tok ÷ 244,67 t/s  ≈  12,3 s
geração    300 tok ÷  22,86 t/s  ≈  13,1 s
                                    ──────
por documento                       ≈  25 s
```

**Consequência:** o PDF de 1,5 GB do `EXTRACAO_ROADMAP.md` (2.765 docs, **~13 min**
no pod 5090) sairia em **~19 h** no M4. `N_PARALLEL` não resolve — numa GPU só as
chamadas disputam o mesmo hardware; paralelismo esconde latência, não cria
throughput.

> **Portanto: o M4 NÃO substitui o pod 5090 pra demo ao vivo.** Numa showcase de
> investidor, um PDF de ~20 docs vira ~8 min de tela parada. O lugar dele é
> **P1 (endpoint adicional no pool)** pra trabalho em lote — extração do acervo
> rodando de madrugada, onde 19h de graça é bom negócio e ninguém olha a tela.

## Stack instalado

```
Xcode CLT 26.6 ──► Homebrew 6.0.15 (/opt/homebrew)
      ├── tailscale 1.102.2   daemon headless (NÃO o app GUI — ver gotchas)
      ├── llama.cpp b10280    Metal 4
      ├── tesseract 5.5.3     + tesseract-lang (por.traineddata) — OCR do SDK
      ├── python@3.12.13
      └── uv

~/extrator-precatorio-sdk/          clone de ubuntu@llmsv2:/mnt/nas-data/git-backups/
      ├── .venv/                    uv venv 3.12 + requirements.txt
      ├── models/*.gguf             3 GGUF (v1, v2, v2.1) — rsync do llmsv2
      └── deploy/serve-macos.sh     supervisor (espelha deploy/serve.sh, mas Metal)
```

### Onde ficam os pesos

```bash
# origem (llmsv2)
/mnt/nas-data/voyager-train/out/precatorio-extrator-q4_k_m.gguf      # v1
/mnt/nas-data/voyager-train/out/precatorio-extrator-v2-q4_k_m.gguf   # v2
/mnt/nas-data/voyager-train/out/extrator-v21-Q4_K_M.gguf             # v2.1 CAMPEÃO
```

Puxar com `rsync -a --partial --inplace` (retomável — o link DERP cai). ~13 MB/s,
~17 min pros 3. Conferir md5 contra [`MODELOS.md`](MODELOS.md) — v2.1 é
`0012607b1634e7b8f96c8f6a9d7bad21`.

## Validação ponta a ponta (2026-08-07)

Ofício requisitório sintético de 1 página, `POST /extrair` no v1:

```
docs:    ['OFICIO_REQUISITORIO']
fichas:  1 → JOAO DA SILVA SANTOS · papel BENEFICIARIO · confiança alta
tempos:  {'total_s': 9.381, 'llm_s': 9.36, 'texto_ocr_s': 0.022, 'n_docs': 1}
```

Classificação, extração, merger e proveniência funcionando em Metal. Os 9,4 s são
de **1 doc curto** (~250 tokens) — a janela cheia de 9k chars é bem mais cara
(ver seção de desempenho).

O campeão v2.1 no mesmo PDF (`:8003`) — **extraiu o valor**, que o v1 abstém:

```
tempos:  {'total_s': 7.428, 'llm_s': 7.411}
ficha:   JOAO DA SILVA SANTOS | BENEFICIARIO | valor_a_receber 157800.65
```

md5 conferidos contra [`MODELOS.md`](MODELOS.md) após o rsync via DERP — **os 3 batem**:

| Versão | md5 | Confere |
|---|---|---|
| v1 | `01cd53ff77ae9f76d5c360a4bec1ebf2` | ✅ |
| v2 | `59db32db31df1ebda640440332321de3` | ✅ |
| v2.1 | `0012607b1634e7b8f96c8f6a9d7bad21` | ✅ |

### Memória com os 3 modelos carregados (medido)

```
llama-server RSS total: 16,1 GB      ← estimativa teórica era 15,9 GB
memória livre do sistema: 27%
pageouts: 33                          ← praticamente sem swap
```

A conta de `-c 16384` fechou. Com `-c 32768` (o do pod) seriam ~18,7 GB e o
sistema entraria em pressão real.

## Serving

`deploy/serve-macos.sh` espelha o `deploy/serve.sh` do pod, com 3 diferenças:

| | pod (CUDA) | Mac (Metal) |
|---|---|---|
| offload | `CUDA_VISIBLE_DEVICES=0 -ngl 999` | `-ngl 999` (sem env de device) |
| contexto | `-c 32768 --parallel 2` | **`-c 16384 --parallel 2`** |
| bind | `0.0.0.0` (porta pública) | **IP do Tailscale** (nunca a LAN) |

Portas idênticas às do pod — trocar de host é trocar só o IP:

```
v1   : llm 8081  api 8001
v2   : llm 8082  api 8002
v2.1 : llm 8083  api 8003
```

```bash
ssh davicordeiro@192.168.200.37
~/extrator-precatorio-sdk/deploy/serve-macos.sh up|status|down     # manual (nohup)
```

### Sobe sozinho no boot — `launchd`

O `serve-macos.sh` é pra uso manual: usa `nohup`, então **não sobrevive a reboot**.
O que sobe sozinho é o conjunto de **LaunchDaemons** (não Agents → não dependem de
login):

```bash
sudo ~/extrator-precatorio-sdk/deploy/macos/install-daemons.sh install|status|uninstall
```

| Daemon | O que faz |
|---|---|
| `br.dev.was.extrator.gpumem` | one-shot: reaplica `iogpu.wired_limit_mb=20480` (o sysctl não persiste) |
| `br.dev.was.extrator.llm.<v>` | `llama-server` Metal — `KeepAlive`, respawn automático |
| `br.dev.was.extrator.api.<v>` | `uvicorn` do SDK — `KeepAlive` |

Wrappers em `deploy/macos/`: `llm-run.sh`, `api-run.sh` e `ts-ip.sh`.

> **Por que `ts-ip.sh`:** no boot o `tailscaled` pode ainda não ter IP quando os
> daemons sobem — o bind falharia e o launchd entraria em respawn loop. O wrapper
> espera até 120 s pelo IP do Tailscale e só então faz `exec`, com fallback pra
> `127.0.0.1`.

Estado após `install` (2026-08-07):

```
bind: 100.105.16.107   wired_limit: 20480 MB
v1   llm:8081(OK)  api:8001(OK)
v2   llm:8082(OK)  api:8002(OK)
```

> **Gotcha:** não instale os daemons com GGUF ainda em transferência. O
> `llama-server` sobe com arquivo truncado, o `KeepAlive` mascara o crash e o
> `/health` pode responder OK com peso incompleto. Depois de completar o rsync:
> `sudo launchctl kickstart -k system/br.dev.was.extrator.llm.<v>`.

### Por que `-c 16384` e não `32768`

Os 3 modelos dividem **os mesmos 24 GB unificados** — não são 3 GPUs. KV cache do
Qwen2.5-7B (28 camadas, 4 KV heads × 128, f16) ≈ **57 KB/token**:

```
-c 32768 → KV 1,88 GB/modelo → 3 × (4,36 + 1,88) = 18,7 GB   ← estoura o teto
-c 16384 → KV 0,94 GB/modelo → 3 × (4,36 + 0,94) = 15,9 GB   ← cabe
```

`16384 / --parallel 2` = **8192 tok/slot**, bem acima dos ~3k da janela do SDK —
longe do `HTTP 400` em doc denso registrado no [`TREINAMENTOS.md`](TREINAMENTOS.md) §5.

## Gotchas do macOS (todos custaram tempo)

**1. O app GUI do Tailscale não serve — use o do brew.**
O `Tailscale-*-macos.pkg` instala uma **Network Extension** que fica
`[activated waiting for user]` até alguém clicar em *System Settings → General →
Login Items & Extensions*. Não há caminho por SSH (só MDM). O `tailscaled` do
Homebrew é o daemon open-source: cria `utun` como root, **zero extension, zero GUI**.

```bash
brew install tailscale && sudo brew services start tailscale
sudo tailscale up --hostname=voyager-worker-mac --accept-routes
```

`sudo brew services` instala **LaunchDaemon** (não Agent) → sobe no **boot**, antes
de qualquer login. **Verificado num reboot real** (2026-08-07 14:37): voltou como pid 310.

**1b. Se o app GUI TAMBÉM for instalado, viram DOIS tailscaled — e a CLI mente.**
Descoberto no reboot de 2026-08-07: alguém aprovou a Network Extension do app, e a
máquina passou a ter duas identidades no tailnet ao mesmo tempo:

```
utun0  100.105.16.107  tailscaled do brew    nó "voyager-worker-mac"  ← serviços aqui
utun5  100.88.162.109  app GUI (macsys ext)  nó "mac-mini-de-davi"    ← nada escutando
```

Sintoma 1: `tailscale ip -4` responde o IP do **macsys**, com aviso de versão
divergente (`client 1.102.2-teb67e5dcb != server 1.102.2-t6cac91817`). No próximo
boot o `ts-ip.sh` bindaria os serviços no IP errado.

**Fix:** o `ts-ip.sh` fixa o socket do brew, nunca a CLI default.

```bash
tailscale --socket /var/run/tailscaled.socket ip -4   # → 100.105.16.107 (certo)
tailscale ip -4                                        # → 100.88.162.109 (macsys)
```

#### Sintoma 2 (grave): a máquina fica INALCANÇÁVEL de fora, mas `tailscale ping` passa

Os dois stacks disputam a tabela de rotas e ela fica **partida**:

```
100.64/10          → utun0    ← daemon do brew, onde os serviços escutam
100.100.144.57/32  → utun1    ← rota pro .103 sai pelo stack ERRADO
100.68.5.114/32    → utun1
```

Pacote entra pelo `utun0`, o serviço responde, a resposta é roteada pelo `utun1`
— outra identidade, sem estado da conexão — e se perde. **Toda** porta fica
bloqueada de fora, inclusive a 22.

O que engana: `tailscale ping` **funciona** (`pong ... via DERP(sao) in 5ms`)
porque é resolvido dentro do daemon, sem passar pela pilha IP. Fácil concluir
"a rede está boa" e procurar no lugar errado. Diagnóstico honesto é TCP puro:

```bash
# no .103
timeout 6 bash -c '</dev/tcp/100.105.16.107/8003' && echo aberto || echo bloqueado
route -n get 100.100.144.57 | grep interface   # no Mac: tem que ser utun0
```

**Fix (encerrar o app NÃO basta** — a system extension é independente e segue
de pé com o utun ativo):

```bash
osascript -e 'quit app "Tailscale"'
sudo tailscale logout          # SEM --socket → atinge o macsys, derruba o utun dele
route -n get 100.100.144.57 | grep interface    # deve voltar pra utun0
```

Depois disso, de prod: `:8001 :8002 :8003 → http=200`.

Ideal continua sendo remover o app (`/Applications/Tailscale.app`) e a system
extension de vez — o `logout` resolve até alguém logar no app de novo.

**2. `tailscale up` não imprime a URL em pipe.** Sem TTY o output fica preso. Pegue assim:

```bash
tailscale status --json | tr ',' '\n' | grep -i authurl
```

E o link **morre se a máquina reiniciar** antes de você clicar — gere outro.

**3. A CLI do app GUI precisa de wrapper, não symlink.** Symlink quebra a resolução
do bundle (`Fatal error: The current bundleIdentifier is unknown to the registry`).
O instalador oficial cria um script `exec`. Irrelevante se você usar o brew.

**4. `iogpu.wired_limit_mb` NÃO persiste no boot.** Volta a `0` (default ~75% da RAM
= ~18 GB). Pra 3 modelos é apertado:

```bash
sudo sysctl -w iogpu.wired_limit_mb=20480
```

⚠️ **Falta LaunchDaemon pra reaplicar isso no boot** — ver Pendências.

**5. VNC/Screen Sharing não liga por SSH.** O `kickstart` do ARD responde literalmente
`Screen Sharing or Remote Management must be enabled from System Settings or via MDM`.
O TCC do macOS 26 exige clique presencial em *System Settings → General → Sharing*.

**6. Sem `nproc`/`free`.** Use `sysctl -n hw.ncpu hw.memsize` e `vm_stat`.

**7. `python3` sem CLT dispara o instalador do Xcode.** Instale o CLT primeiro:

```bash
touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
softwareupdate -i "Command Line Tools for Xcode 26.6-26.6"   # label EXATO, com sufixo
```

## Energia (worker não dorme)

```bash
sudo pmset -a sleep 0 disksleep 0 womp 1 autorestart 1
```

`autorestart 1` = religa após queda de energia. `womp 1` = wake-on-LAN.
Essas **persistem** no boot (diferente do `sysctl`).

## Suíte do SDK no Mac

```
158 passed, 3 failed, 1 skipped
```

As 3 falhas (`test_instrucoes_cobrem_as_7_tarefas`, `test_pipeline_monta_ficha_com_
proveniencia`, `test_pipeline_estagio_emitido_e_timeline_ancorada`) são **drift do
próprio repo** (`INSTRUCAO` declara 7 tarefas a mais que `TAREFAS`) — comparação
entre duas constantes do código, nada de plataforma. Já vinham assim no `ecccca3`.

## Como plugar no pool multi-GPU (P1)

O `deploy/serve.sh` do pod **já tem** o modo `llm-only` ("pod B: só GPU, sem api"):

```bash
# no Mac — só a GPU, sem FastAPI
~/extrator-precatorio-sdk/deploy/serve-macos.sh up     # ou llama-server direto

# no host que roda a API — adiciona o Mac ao pool
LLM_URLS_v2_1="http://127.0.0.1:8083,http://100.105.16.107:8083" \
LLM_LABELS_v2_1="RTX5090-podA,M4-mac" \
  /opt/extrator/extrator-precatorio-sdk/deploy/serve.sh up
```

O `LlamaClient` faz round-robin entre os endpoints (commit `ecccca3`).
**Cuidado com o balanceamento:** round-robin puro manda 50% das janelas pro M4,
que é ~1 ordem de grandeza mais lento — o lote inteiro passa a andar na velocidade
dele. Pro pool render, o balanceio precisa ser **least-in-flight**, não round-robin
cego (o cliente suporta os dois; conferir qual está ativo).

## O que sobe sozinho no boot — **testado num reboot real** (2026-08-07 15:07)

| Componente | Sobe sozinho? | Mecanismo | PID pós-boot |
|---|---|---|---|
| `tailscaled` | ✅ | LaunchDaemon do brew | 315 |
| `iogpu.wired_limit_mb` | ✅ | `br.dev.was.extrator.gpumem` | one-shot (20480 ✓) |
| `llama-server` × 3 | ✅ | `br.dev.was.extrator.llm.*` (`KeepAlive`) | 311, 313, 314 |
| SDK FastAPI × 3 | ✅ | `br.dev.was.extrator.api.*` (`KeepAlive`) | 308, 309, 312 |
| Energia (não dorme) | ✅ | `pmset` (persiste em NVRAM) | — |

PIDs 308-315 = subiram no começo do boot, **sem ninguém logar**. Nenhum é
LaunchAgent → não dependem de login, não precisa de autologin.

**2º reboot (15:19) — validação da persistência.** O 1º reboot quebrou duas coisas
(IP do Wi-Fi por DHCP e IP do Tailscale resolvido pelo daemon errado); o 2º
confirmou os dois fixes:

| | Antes | Depois |
|---|---|---|
| Wi-Fi `en1` | `192.168.200.37` | `192.168.200.37` (`Manual Configuration`) ✅ |
| `ts-ip.sh` | `100.105.16.107` | `100.105.16.107` ✅ |
| `iogpu.wired_limit_mb` | 20480 | 20480 ✅ |
| daemons | 7 | 7 ✅ |
| logados | 0 | 0 ✅ |

Extração real pós-reboot no v2.1: `157800.65`, confiança alta, **7,43 s** —
idêntico ao pré-reboot.

> ⚠️ **`/tmp` é limpo no boot do macOS.** PDFs de teste e logs de provisionamento
> ali somem. Fixture fica em `~/fixtures/oficio_full.pdf`.
>
> Pior: `curl -F "files=@arquivo_inexistente"` **não falha alto** — o SDK recebe
> conteúdo vazio, classifica como `DESPACHO` e devolve 0 fichas em ~0,2 s. Parece
> resposta válida. Sempre confira o `ls -l` do fixture antes de concluir qualquer
> coisa de um teste rápido.

## Alcance a partir de prod (o teste que vale)

Testar do laptop não prova nada — quem precisa alcançar o Mac é o host `web`
(`.103`). Validado em 2026-08-07 **depois** de resolver o conflito de rotas:

```
.103 → 100.105.16.107:800{1,2,3}  →  http=200
.103 → POST :8003/extrair (ofício requisitório de 1 página)
       round-trip 7,44 s · modelo 7,39 s (rede ≈ 45 ms)
       JOAO DA SILVA SANTOS | BENEFICIARIO | 157800.65 | conf: alta
```

Pra apontar a showcase pro Mac:

```python
# core/settings.py :: _SHOWCASE_DEFAULT  (ou env SHOWCASE_MODELOS em JSON)
"v1":  {"url": "http://100.105.16.107:8001", ...}
"v2":  {"url": "http://100.105.16.107:8002", ...}
"v21": {"url": "http://100.105.16.107:8003", ...}
```


Após o boot, `install-daemons.sh status` deu:

```
bind: 100.105.16.107   wired_limit: 20480 MB
v1   llm:8081(OK)  api:8001(OK)
v2   llm:8082(OK)  api:8002(OK)
v21  llm:8083(OK)  api:8003(OK)
```

## Pendências

- [ ] Decidir o papel definitivo (substituir pod × pool P1) — ver seção de desempenho
- [ ] **Remover o app GUI do Tailscale** (duas identidades no tailnet — ver gotcha 1b)
- [ ] Teste de carga real: N janelas concorrentes de 9k chars (o número de 25 s/doc
      é derivado do `llama-bench`, não medido sob concorrência)
- [ ] Senha VNC legacy `davicord` setada via `kickstart` — remover se Screen Sharing for ligado

**Decidido:** `/etc/sudoers.d/voyager-setup` (NOPASSWD pro `davicordeiro`) **fica** —
é o que permite automatizar `launchctl`/`sysctl`/`networksetup` por SSH sem TTY.

**Nota:** a chave `voyager-worker-mac` está autorizada em `~ubuntu/.ssh/authorized_keys`
do llmsv2 — é o que permite o Mac puxar GGUF e clonar o SDK sem passar pelo laptop.
