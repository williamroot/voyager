"""Testes das funções PURAS de render do export da showcase (MD/HTML/PDF/JSON).

Sem DB nem rede — só o payload → arquivo. Cobre o caso rico (com estágio +
status), a forward-compat (campos ausentes não quebram) e o filename.
"""
import json

import pytest

from dashboard import showcase_export as se

PAYLOAD_RICO = {
    "versao": "v22",
    "label": "v2.2 (herdeiros)",
    "elapsed_ms": 8421,
    "tempos": {"total_s": 7.9, "paginas_ocr": 3},
    "arquivo": "Cumprimento de Sentença — Espólio.pdf",
    "fichas": [
        {"nome": "João da Silva (Espólio)", "papel": "ESPOLIO", "cpf_cnpj": "123.456.789-00",
         "valor_a_receber": {"valor": "487.320,55", "status": "a_receber", "status_rotulo": "A receber"}},
        {"nome": "Maria da Silva", "papel": "HERDEIRO", "cpf_cnpj": "987.654.321-00",
         "valor_a_receber": {"valor": 162440.18, "status": "recebido", "status_rotulo": "Recebido"},
         "recebido": [{"valor": "162.440,18"}]},
        {"nome": "Ana", "papel": "HERDEIRO",
         "valor_a_receber": {"abstido": True, "motivo_abstencao": "sem valor"}},
        {"nome": "Estado de São Paulo", "papel": "EXECUTADO", "cpf_cnpj": "46.377.222/0001-29"},
    ],
    "docs": [
        {"classe": "sentença", "registros": [{"natureza": "ALIMENTAR", "ente_devedor": "Estado de SP"}]},
        {"classe": "cálculo homologado"}, {"classe": "cálculo homologado"},
    ],
    "avisos": [{"texto": "espólio com partilha"}],
    "estagio": {
        "rotulo": "EMITIDO", "descricao": "Precatório expedido.",
        "linha_do_tempo": [
            {"data": "2019-03-11", "titulo": "Distribuição", "status": "concluido"},
            {"data": "2025-12-31", "titulo": "Pagamento previsto", "status": "pendente"},
        ],
        "proximos_passos": ["Habilitar herdeiros", "Acompanhar ordem cronológica"],
    },
}

PAYLOAD_MIN = {
    "versao": "v1", "label": "Geração 1", "arquivo": "doc.pdf",
    "fichas": [{"nome": "Fulano", "papel": "EXEQUENTE", "valor_a_receber": {"valor": 12000}}],
    "docs": [{"classe": "petição"}],
}


# ── helpers puros ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,esperado", [
    ("487.320,55", 487320.55), (162440.18, 162440.18), ("R$ 1.234,56", 1234.56),
    ("", None), (None, None), ("abc", None),
])
def test_num(raw, esperado):
    assert se._num(raw) == esperado


def test_money_ptbr():
    assert se._money(1234567.5) == "R$ 1.234.567,50"
    assert se._money(None) == "—"


def test_valor_receber_soma_lista():
    p = {"valores_a_receber": [{"valor": "100,00"}, {"valor": 50}]}
    assert se._valor_receber(p) == 150.0


def test_resumo_credito_soma_e_natureza_ente():
    r = se._resumo_credito(PAYLOAD_RICO)
    assert r["valor_total"] == pytest.approx(487320.55 + 162440.18)
    assert r["natureza"] == "ALIMENTAR"
    assert r["ente"] == "Estado de SP"       # dos registros do doc
    assert r["estagio_rotulo"] == "EMITIDO"
    assert r["n_partes"] == 4


def test_docs_lidos_agrega_por_classe():
    docs = dict(se._docs_lidos(PAYLOAD_RICO))
    assert docs["cálculo homologado"] == 2
    assert docs["sentença"] == 1


def test_marcos_e_passos():
    est = PAYLOAD_RICO["estagio"]
    marcos = se._marcos(est)
    assert len(marcos) == 2 and marcos[0]["data"] == "2019-03-11"
    assert se._proximos_passos(est) == ["Habilitar herdeiros", "Acompanhar ordem cronológica"]


# ── filename ─────────────────────────────────────────────────────────────────

def test_base_filename_sanitiza_e_timestamp():
    fn = se.base_filename(PAYLOAD_RICO)
    assert fn.startswith("analise-cumprimento-de-sentenca-espolio")
    assert fn[-1].isdigit()                  # timestamp no fim
    assert " " not in fn and ".pdf" not in fn


# ── render MD ────────────────────────────────────────────────────────────────

def test_render_md_conteudo():
    md = se.render_md(PAYLOAD_RICO)
    assert "# Análise da extração" in md
    assert "| Nome | Papel | Documento | A receber | Status |" in md
    assert "João da Silva (Espólio)" in md and "A receber" in md
    assert "modelo absteve" in md            # abstenção
    assert "## Estágio do crédito" in md and "### Linha do tempo" in md
    assert "Habilitar herdeiros" in md
    assert "sem consulta externa" in md      # rodapé on-device
    assert "cálculo homologado ×2" in md


def test_render_md_forward_compat():
    """Sem estagio nem status: não pode quebrar."""
    md = se.render_md(PAYLOAD_MIN)
    assert "Fulano" in md
    assert "## Estágio do crédito" not in md  # bloco some se ausente


def test_render_md_vazio():
    assert se.render_md({}).strip()           # payload {} não levanta


# ── render HTML ──────────────────────────────────────────────────────────────

def test_render_html_escapa_e_status():
    payload = dict(PAYLOAD_RICO)
    payload["fichas"] = [{"nome": "<script>alert(1)</script>", "papel": "EXEQUENTE",
                          "valor_a_receber": {"valor": 10, "status_rotulo": "A receber"}}]
    h = se.render_html(payload)
    assert "<script>alert(1)</script>" not in h          # escapado
    assert "&lt;script&gt;" in h
    assert "A receber" in h and "application/pdf" not in h


def test_render_html_forward_compat_vazio():
    assert "<html" in se.render_html({}).lower()


# ── JSON reversível ──────────────────────────────────────────────────────────

def test_json_pretty_reversivel():
    body = json.dumps(PAYLOAD_RICO, ensure_ascii=False, indent=2, default=str)
    assert json.loads(body)["estagio"]["rotulo"] == "EMITIDO"
    assert "\n" in body                       # pretty
