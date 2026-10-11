"""La normalización debe colapsar variantes y borrar nombres de personas."""

from moooney.categorize.normalize import clean_concept


def test_misma_tienda_distinto_terminal_misma_clave():
    a = "PAGO MOVIL EN SUPERMERCADOS LA ESQUINA, PORRIÑO, O ES, TARJ. :*0081 5810 0205 1234"
    b = "PAGO MOVIL EN SUPERMERCADOS LA ESQUINA, PORRIÑO, TARJ. :*0081 5810 0205 9876"
    assert clean_concept(a) == clean_concept(b) == "SUPERMERCADOS LA ESQUINA"


def test_compra_por_internet_con_pan_y_comision():
    a = "COMPRA ZAPATOS EL BUEN ANDAR, MADRID, TARJETA 5163830303900205 , COMISION 0,00"
    b = "COMPRA ZAPATOS EL BUEN ANDAR, MADRID, TARJETA 4153830303911111 , COMISION 1,50"
    assert clean_concept(a) == clean_concept(b) == "ZAPATOS EL BUEN ANDAR"


def test_recibo_domiciliado_corta_el_concepto():
    # El marcador RECIBO introduce datos del adeudo (mandato, referencia), no el comercio.
    a = "SUPERMERCADOS LA ESQUINA RECIBO DIRECTO 12/2026 MANDATO 5ZF000"
    b = "SUPERMERCADOS LA ESQUINA RECIBO DOMICILIADO REF 00987"
    assert clean_concept(a) == clean_concept(b) == "SUPERMERCADOS LA ESQUINA"


def test_bizum_sin_nombre_de_persona():
    assert clean_concept("BIZUM A FAVOR DE Ana Pérez Sancho CONCEPTO: CENA") == "BIZUM ENVIADO"
    assert clean_concept("BIZUM DE María Do Val Río CONCEPTO: BIEN") == "BIZUM RECIBIDO"


def test_bizum_a_comercio_conserva_comercio():
    assert clean_concept("COMPRA BIZUM PIZZERIA EL HORNO CONCEPTO: CENA") == (
        "BIZUM ENVIADO PIZZERIA EL HORNO"
    )


def test_transferencia_a_particular_sin_nombre():
    a = "TRANSFERENCIA A FAVOR DE Laura Vidal Rúa CONCEPTO: FESTA"
    assert clean_concept(a) == "TRANSFERENCIA A PARTICULAR"
    b = "TRANSFERENCIA DE Juan Gómez López, . CONCEPTO DEVOLUCION FESTA"
    assert clean_concept(b) == "TRANSFERENCIA DE PARTICULAR"


def test_devolucion_de_hacienda_identificable():
    a = "TRANSFERENCIA DE DEVOLUCIONES TRIBUTARIAS, AGENCIA ESTATAL AEAT, CONCEPTO BORRADOR"
    assert clean_concept(a) == "TRANSFERENCIA DE PARTICULAR AEAT"


def test_nomina_aunque_venga_en_transferencia():
    a = "TRANSFERENCIA DE Empresa Ejemplo Sociedad Limitada CONCEPTO ABONO NOMINA 01/2025"
    assert clean_concept(a) == "NOMINA"


def test_traspasos_a_hucha_colapsan():
    a = "TRASPASO: AHORRO HUCHA OBJETIVO: LETRAS PEQUENAS"
    b = "TRANSFERENCIA A FAVOR DE Uno Dos Tres CONCEPTO AHORRO HUCHA OBJETIVO: VIAJE"
    assert clean_concept(a) == clean_concept(b) == "TRASPASO HUCHA"
    assert clean_concept("TRASPASO: CUENTA REMUNERADA 123") == "TRASPASO INTERNO"


def test_liquidacion_tarjeta_y_efectivo():
    a = "LIQUIDACION DE LAS TARJETAS DE CREDITO DEL CONTRATO 0049 5937 502 1933866 1"
    assert clean_concept(a) == "LIQUIDACION DE TARJETAS DE CREDITO"
    b = (
        "RETIRADA DE EFECTIVO EN CAJERO AUTOMATICO 1234 EL 05/01/2025 A LAS 20:15"
        "..PAN:5163830303900205"
    )
    assert clean_concept(b) == "RETIRADA DE EFECTIVO EN CAJERO"


def test_devolucion_conserva_prefijo_y_comercio():
    a = "DEVOLUCION PAGO MOVIL EN LIBROS LA PGINA, SANTIAGO DE COMPOSTELA, TARJ. :*0081 2"
    assert clean_concept(a) == "DEVOLUCION LIBROS LA PGINA"


def test_vacio_y_ruido():
    assert clean_concept("") == "SIN CLAVE"
    assert clean_concept("   ") == "SIN CLAVE"
    assert clean_concept("COMPRA") == "SIN CLAVE"


def test_acentos_y_mayusculas_no_rompen_la_clave():
    a = "pago móvil en A Padenda do Pan, Porriño, ES, TARJ. :*0081 1"
    b = "PAGO MOVIL EN A PALENCA DO PAN, VIGO, ES, TARJ. :*0081 2"
    assert clean_concept(a) == "A PADENDA DO PAN"
    assert clean_concept(b) == "A PALENCA DO PAN"


def test_prefijos_contactless_y_procesadores_de_pago():
    assert clean_concept("TRANSACCION CONTACTLESS EN TASCA DO RIO VIGO") == "TASCA DO RIO VIGO"
    assert clean_concept("SUMUP LA TASQUITA TA") == "LA TASQUITA TA"
    assert clean_concept("ZETTLE BAR PEPE") == "BAR PEPE"


def test_marcador_al_principio_no_deja_la_clave_vacia():
    assert clean_concept("COMISION MANTENIMIENTO CUENTA") == "COMISION MANTENIMIENTO CUENTA"
    assert clean_concept("COMISION DE MANTENIMIENTO") == "COMISION DE MANTENIMIENTO"
    # en medio sigue cortando: el comercio es lo de antes
    assert clean_concept("TIENDA FALSA TARJETA 1234") == "TIENDA FALSA"
