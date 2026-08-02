from tribunals.tipos_norm import TIPOS_NORM, classificar_tipo_norm


class _MockMov:
    def __init__(self, tipo_comunicacao='', texto=''):
        self.tipo_comunicacao = tipo_comunicacao
        self.texto = texto


def test_fixture_tem_tuplas():
    assert len(TIPOS_NORM) > 40
    assert (23, 1, 'Disponibilizada a Intimação') in TIPOS_NORM


def test_classificar_intimacao():
    mov = _MockMov(tipo_comunicacao='Intimação', texto='Disponibilizada a intimação da parte autora')
    result = classificar_tipo_norm(mov)
    assert (23, 1) in result


def test_classificar_sentenca_procedente():
    mov = _MockMov(tipo_comunicacao='Sentença', texto='Julgo PROCEDENTE o pedido')
    result = classificar_tipo_norm(mov)
    assert (17, 1) in result


def test_classificar_acordao():
    mov = _MockMov(tipo_comunicacao='Acórdão', texto='Publicado o acórdão')
    result = classificar_tipo_norm(mov)
    assert (25, 1) in result


def test_classificar_transitado_julgado():
    mov = _MockMov(tipo_comunicacao='', texto='Transitado em julgado')
    result = classificar_tipo_norm(mov)
    assert (18, 1) in result


def test_classificar_audiencia_designada():
    mov = _MockMov(tipo_comunicacao='', texto='Audiência designada')
    result = classificar_tipo_norm(mov)
    assert (2, 1) in result


def test_classificar_citacao_positiva():
    mov = _MockMov(tipo_comunicacao='', texto='Citação positiva')
    result = classificar_tipo_norm(mov)
    assert (30, 1) in result


def test_classificar_despacho_generico():
    mov = _MockMov(tipo_comunicacao='Despacho', texto='despacho generico')
    result = classificar_tipo_norm(mov)
    # despacho casa (21, 0) por design
    assert (21, 0) in result


def test_classificar_vazio_retorna_lista_vazia():
    mov = _MockMov(tipo_comunicacao='', texto='')
    result = classificar_tipo_norm(mov)
    assert result == []