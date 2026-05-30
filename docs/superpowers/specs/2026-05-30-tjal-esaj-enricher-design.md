# TJAL e-SAJ enricher — design

Data: 2026-05-30
Branch: `feat/enricher-tjal-esaj`

## Objetivo

Adicionar enricher de consulta pública para o **TJAL** (Tribunal de Justiça de
Alagoas), que roda o mesmo software **e-SAJ** do TJSP. Reaproveitar toda a
lógica já provada em `enrichers/esaj.py` (hoje `TjspEnricher`), generalizando os
pontos hardcoded de TJSP.

## Contexto descoberto

- `enrichers/esaj.py` tem TJSP cravado em 2 lugares:
  1. `BASE_URL = 'https://esaj.tjsp.jus.br'`.
  2. `ndo = cnj_fmt.split('.8.26')[0]` — `.8.26` é o segmento `J.TR` do TJSP
     (justiça estadual 8, tribunal 26 = SP).
- TJAL roda e-SAJ idêntico em `https://www2.tjal.jus.br/cpopg/` (verificado
  2026-05-30). Segmento CNJ do TJAL = `.8.02`.
- O enricher e-SAJ **não tem teste** hoje. TJAL entra com cobertura e-2-e e
  trava o contrato do parser.
- 7 pontos de wiring para um tribunal novo (padrão já seguido por TJMA/TJDFT):
  registry `_ENRICHERS`, `RQ_QUEUES`, `TRIBUNAIS_COM_ENRICHER`, botão do
  dashboard, seed migration, `worker_<sigla>` no compose de prod, docs `.ia`.

## Design

### 1. Refatorar `esaj.py` em base + subclasses

Espelha o padrão `BasePjeEnricher` (uma base com toda a lógica, subclasses só
configuram).

```python
class BaseEsajEnricher:
    BASE_URL = None          # subclasse obrigatória
    TRIBUNAL_SIGLA = None    # subclasse obrigatória
    LOG_NAME = 'voyager.enrichers.esaj'
    # OPEN_URL / SEARCH_URL derivam de BASE_URL em __init__ (ou property)
    # toda a lógica de _fetch_processo / _extrair_dados / _extrair_partes aqui

class TjspEnricher(BaseEsajEnricher):
    BASE_URL = 'https://esaj.tjsp.jus.br'
    TRIBUNAL_SIGLA = 'TJSP'
    LOG_NAME = 'voyager.enrichers.tjsp'

class TjalEnricher(BaseEsajEnricher):
    BASE_URL = 'https://www2.tjal.jus.br'
    TRIBUNAL_SIGLA = 'TJAL'
    LOG_NAME = 'voyager.enrichers.tjal'
```

**Generalizar o split do CNJ** (independente de tribunal): a partir de
`cnj_fmt = NNNNNNN-DD.AAAA.J.TR.OOOO`:

```python
parts = cnj_fmt.split('.')         # ['NNNNNNN-DD','AAAA','J','TR','OOOO']
ndo  = f'{parts[0]}.{parts[1]}'    # numeroDigitoAnoUnificado
foro = parts[4]                    # foroNumeroUnificado
```

Isso produz exatamente os mesmos valores que o split antigo `.8.26` produzia
para TJSP (regressão coberta por teste) e funciona para `.8.02` (TJAL).

Construtor valida `BASE_URL`/`TRIBUNAL_SIGLA` setados (NotImplementedError se
faltar) — igual à `BasePjeEnricher`.

### 2. Wiring (7 edits)

| Arquivo | Mudança |
|---|---|
| `enrichers/jobs.py` | `from .esaj import TjalEnricher`; `_ENRICHERS['TJAL'] = TjalEnricher` |
| `core/settings.py` | `'enrich_tjal'` em `RQ_QUEUES` (timeout 600) |
| `djen/ingestion.py` | `'TJAL'` em `TRIBUNAIS_COM_ENRICHER` |
| `dashboard/.../processo_detail.html` | adicionar `tribunal_id == 'TJAL'` na condição do botão |
| `tribunals/migrations/0036_seed_tjal.py` | `update_or_create` TJAL, `ativo=False` |
| `docker-compose-prod.yml` | serviço `worker_tjal` (replicas: 4) |
| `.ia/ENRICHMENT.md` + `.ia/OVERVIEW.md` | registrar TJAL |

### 3. Teste e-2-e (`tests/test_enricher_tjal.py`)

Espelha `tests/test_enricher_tjma.py`. Sem DB nem Redis — `stream.publish`
interceptado, HTTP da `requests.Session` mockado. Fixture e-SAJ
**sintetizada** (decisão do usuário 2026-05-30) em `tests/fixtures/tjal/`,
fiel à estrutura real do e-SAJ (mesmos seletores `#classeProcesso`,
`#tablePartesPrincipais`, `.tipoDeParticipacao`, `.nomeParteEAdvogado` já
validados no TjspEnricher).

Casos:
1. **Config**: URLs/sigla/log do `TjalEnricher`; `issubclass(TjalEnricher, BaseEsajEnricher)`; construtor incompleto quebra.
2. **Wiring**: `_ENRICHERS['TJAL']`, `queue_for('TJAL')=='enrich_tjal'`, fila em settings, `'TJAL' in TRIBUNAIS_COM_ENRICHER`, botão no template.
3. **Generalização CNJ**: `_format_cnj` + split produz `foro`/`ndo` corretos pra `.8.02` (TJAL) **e** `.8.26` (TJSP — regressão).
4. **Fluxo `enriquecer()` OK**: search 302→show.do mockado → parse → 1 payload `ok` no stream com `dados` (classe/assunto/órgão/data/valor) + `partes` (polo ativo/passivo, advogado com OAB, doc mascarado preservado vazio).
5. **Não encontrado**: search sem redirect / form de busca → payload `nao_encontrado`, sem 2º fetch.
6. **Tribunal errado**: `enriquecer(proc TRF1)` levanta `EsajEnricherError`.

## Não-objetivos (YAGNI)

- Ativar TJAL em prod (`ativo=True`), descobrir floor, disparar backfill — é
  procedimento operacional manual (OPS.md), fora desta PR.
- Captura de fixture viva do TJAL (sem CNJ real / rede no CI).
- Datajud/classificação — automáticos pós-ingestão, sem código novo aqui.

## Riscos

- e-SAJ público mascara CPF/CNPJ → `documento` fica vazio (mesma limitação
  aceita do TJSP). OAB e nome preservados.
- Mapa `tipo → polo` é heurístico; TJAL pode usar abreviações de papel
  diferentes. Mitigado pelo fallback `outros` (nunca perde a parte).
