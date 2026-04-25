# Enriquecimento via consulta pública

DJEN dá só metadata da movimentação (texto, tipo, órgão). Pra **partes** (autores, réus, advogados com OAB) e classe/assunto/valor estruturados, precisamos consultar o sistema do tribunal direto.

## Estado atual

| Tribunal | Sistema | Implementado | Notas |
|---|---|---|---|
| TRF1 | PJe (consulta pública sem login) | **Sim** | `enrichers/trf1.py` |
| TRF3 | PJe v2 | Não | Mesmo motor JSF — copy/adapt |
| TRF2/4/5/6 | PJe (versões variadas) | Não | Mesmo motor |
| TJSP | e-SAJ (sistema próprio) | Não | Backend diferente, parser próprio |

## TRF1 — fluxo (`enrichers/trf1.py`)

```
GET listView.seam ────────► HTML inicial
   │                          │
   │                          ├─ extrai javax.faces.ViewState
   │                          ├─ extrai todos os <input>/<select> do form fPP
   │                          └─ encontra script id dinâmico (executarPesquisaReCaptcha)
   │
POST listView.seam ───────► resposta AJAX (XML/HTML)
   {form_fields, CNJ}        │
                             ├─ regex /consultapublica/.../DetalheProcesso.../...
                             └─ ou idProcessoTrf:NNN → constrói URL fallback
   │
GET detalhe ──────────────► HTML completo
                             │
                             ├─ div.propertyView .name>label + .value
                             │   → classe, assunto, autuação, valor
                             ├─ <b>Órgão Julgador</b><br/>NOME → orgao_julgador
                             └─ div#poloAtivo / div#poloPassivo / div#outrosInteressados
                                 → tabelas com partes
```

**Particularidades:**
- O botão `fPP:searchProcessos` é só um trigger visual — o **script real** com `executarPesquisaReCaptcha` tem id `fPP:j_idXXX` dinâmico. Função `_find_search_script_id` localiza.
- hCaptcha está presente no JS mas com flag `if (false)` — desabilitado por enquanto.
- jsessionid é mantido pelo `requests.Session` (cookie automático).

## Parser de partes (`enrichers/trf1.py::_parse_polo`)

Cada `<tr>` tem múltiplos `<span>`. Estrutura observada:

```
tr[0]  → cabeçalho ("Participante", "Situação")
tr[N]  → dados:
   spans[0] = concatenado (tudo junto)
   spans[1] = parte principal isolada
   spans[2..N-2] = advogados / representantes
   spans[N-1] = situação ("Ativo")
```

Heurísticas:
1. Filtrar `_IGNORE_TEXTOS` (Participante, Situação, Ativo, Inativo, vazio)
2. Detectar concatenado: se `textos[0].count(' - ') >= 2 and textos[1] in textos[0]`, descarta
3. Restantes: primeiro = principal, demais = representantes
4. Pra cada texto, extrair via regex (em `parsers.py`):
   - `parse_documento` → CPF (`\d{3}.\d{3}.\d{3}-\d{2}`) ou CNPJ
   - `parse_oab` → `OAB UF12345`
   - `parse_role` → último `(...)` no texto
   - `limpar_nome` → remove tudo acima

## Persistência (`_aplicar_partes`)

```python
ProcessoParte.objects.filter(processo=processo).delete()  # re-cria todas
for polo, partes in polos.items():
    for principal in partes:
        p_principal = self._upsert_parte(principal)
        # get_or_create — evita duplicate quando mesma parte aparece 2x
        pp_principal, _ = ProcessoParte.objects.get_or_create(
            processo, parte=p_principal, polo, papel,
            representa=None,
        )
        for rep in principal['representantes']:
            p_rep = self._upsert_parte(rep)
            if p_rep == p_principal: continue  # skip auto-rep
            ProcessoParte.objects.create(processo, parte=p_rep, polo, papel='ADVOGADO',
                                          representa=pp_principal)
```

`_upsert_parte` usa `update_or_create`:
- Se `documento != ''`: chave `documento`
- Senão se `oab != ''`: chave `oab`
- Senão: cria novo (aceita duplicata)

## Como adicionar enricher pra outro tribunal

1. Criar `enrichers/<sigla>.py` espelhando `trf1.py`
2. Reusar `parsers.py` (parse_documento, parse_oab, etc.)
3. Reusar `ProxyScrapePool` + `cortex_proxy_url`
4. Implementar:
   - `enriquecer(processo) -> dict`
   - `_buscar_processo` (request shape específico)
   - `_extrair_dados`, `_extrair_partes`
5. Adicionar em `enrichers/jobs.py::_ENRICHERS = {'TRF3': Trf3Enricher, ...}`
6. No template `processo_detail.html`, ampliar a condicional do botão "Atualizar":
   ```html
   {% if processo.tribunal_id in 'TRF1,TRF3'|cut:''|stringformat:'s'|... %}
   ```
   ou criar tag custom `{% if can_enrich processo %}`.

## Comandos

```bash
# Foreground (debug)
docker compose exec web python manage.py enriquecer_processo <CNJ_ou_ID>

# Async (queue default)
docker compose exec web python manage.py enriquecer_processo <CNJ> --async

# Via dashboard: botão "↻ Atualizar dados públicos" no detalhe do processo
```
