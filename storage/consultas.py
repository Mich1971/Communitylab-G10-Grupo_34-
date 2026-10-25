"""Consultas de solo lectura para el panel de curaduría (operaciones 1 a 8).

Cada función es la misma que expone `api/rutas_curaduria.py`: mismos nombres,
parámetros y modelo de respuesta. Reglas que comparten todas:

* Conexión propia con `PRAGMA query_only`, así una lectura jamás escribe y la
  UI puede consultar mientras el worker escribe (la base está en WAL).
* Un fallo de SQLite nunca se convierte en una lista vacía: se lanza
  `LecturaFallida` (HTTP 503), para que la UI no confunda «no pude leer» con
  «no hay datos».
* Las listas llevan el sobre `SobreEstadoRun` con `disponible`/`motivo`, de modo
  que la UI sabe *por qué* una lista viene vacía sin interpretarla.
"""

from __future__ import annotations

import base64
import contextlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

import structlog
from pydantic import ValidationError

from esquemas import (
    CategoriaEnrutamiento,
    PaqueteSalida,
    PuntuacionRelevancia,
    ResumenComunidad,
    Sentimiento,
)
from storage.local_db import _resolver_db_path, reconstruir_json_paquete_salida
from storage.modelos_curaduria import (
    CategoriaEstado,
    ItemEjecucionResumen,
    ItemFuenteOriginal,
    ItemMensajeAnalizadoUI,
    ItemRechazadoUI,
    ItemRevisionResumen,
    MotivoNoDisponible,
    RespuestaDetalleEjecucion,
    RespuestaListarEjecuciones,
    RespuestaListarMensajes,
    RespuestaListarRechazados,
    RespuestaListarRevisiones,
    RespuestaObtenerFuentes,
    RespuestaObtenerResumen,
    RespuestaObtenerRevision,
)

logger = structlog.get_logger("storage.consultas")

TIMEOUT_LECTURA_S = 5.0
ESTADOS = (
    "recibiendo", "en_cola", "procesando", "completado",
    "sin_registros_validos", "sin_contenido_publicable", "fallido",
)
_TERMINALES = ("completado", "sin_registros_validos", "sin_contenido_publicable", "fallido")
_CATEGORIA: dict[str, CategoriaEstado] = {
    "recibiendo": "en_curso",
    "en_cola": "en_curso",
    "procesando": "en_curso",
    "completado": "finalizada_con_paquete",
    "sin_registros_validos": "finalizada_sin_paquete",
    "sin_contenido_publicable": "finalizada_sin_paquete",
    "fallido": "fallida",
}
_ORIGENES = ("validador", "analisis")
_MAX_FUENTES = 50


# ---------------------------------------------------------------------------
# Errores de dominio: la capa HTTP solo los traduce a `{"error", "detalle"}`
# ---------------------------------------------------------------------------
class ErrorCuraduria(Exception):
    codigo = "error_curaduria"
    http_status = 500

    def __init__(self, detalle: str) -> None:
        super().__init__(detalle)
        self.detalle = detalle


class ParametroInvalido(ErrorCuraduria):
    codigo, http_status = "parametro_invalido", 422


class EjecucionNoEncontrada(ErrorCuraduria):
    codigo, http_status = "ejecucion_no_encontrada", 404


class RevisionNoEncontrada(ErrorCuraduria):
    codigo, http_status = "revision_no_encontrada", 404


class LecturaFallida(ErrorCuraduria):
    codigo, http_status = "lectura_fallida", 503


class EscrituraFallida(ErrorCuraduria):
    codigo, http_status = "escritura_fallida", 503


class RevisionInvalida(ErrorCuraduria):
    codigo, http_status = "revision_invalida", 422


class ConflictoRevision(ErrorCuraduria):
    """La revisión base ya no es la última: otra sesión guardó antes."""

    codigo, http_status = "conflicto_revision", 409

    def __init__(self, revision_base: int, revision_actual: int) -> None:
        super().__init__(
            f"La revisión base enviada ({revision_base}) no coincide con la última revisión "
            f"registrada ({revision_actual}). Recargue antes de guardar."
        )
        self.revision_actual = revision_actual


# ---------------------------------------------------------------------------
# Infraestructura
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _lectura(db_path: str | Path | None) -> Iterator[sqlite3.Connection]:
    try:
        con = sqlite3.connect(_resolver_db_path(db_path), timeout=TIMEOUT_LECTURA_S)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON;")
            yield con
        finally:
            con.close()
    except (sqlite3.Error, ValidationError) as exc:
        logger.error("lectura_fallida", error=str(exc)[:300])
        raise LecturaFallida(f"No se pudo leer la base local ({type(exc).__name__}).") from exc


def _tabla_existe(con: sqlite3.Connection, nombre: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;", (nombre,)
    ).fetchone() is not None


def _codificar_cursor(*partes: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(partes).encode()).decode()


def _decodificar_cursor(cursor: str, n: int) -> list[Any]:
    try:
        partes = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if isinstance(partes, list) and len(partes) == n:
            return partes
    except ValueError:
        pass
    raise ParametroInvalido("El parámetro 'cursor' no es válido.")


def _validar_limite(limite: int, maximo: int) -> None:
    if not 1 <= limite <= maximo:
        raise ParametroInvalido(f"El parámetro 'limite' debe estar entre 1 y {maximo} (recibido: {limite}).")


def _validar_opcion(nombre: str, valor: str | None, validos: Sequence[str]) -> None:
    if valor is not None and valor not in validos:
        raise ParametroInvalido(f"El parámetro '{nombre}' debe ser uno de {list(validos)} (recibido: {valor!r}).")


def _en_lista(n: int) -> str:
    return ", ".join("?" * n)


@dataclass(frozen=True)
class _Run:
    run_id: str
    request_id: str
    estado: str
    comunidad: str | None
    periodo: str | None
    fecha_recepcion: str | None
    fecha_procesamiento: str | None
    estado_flujo: str | None
    total_recibidos: int
    total_validos: int
    total_rechazados: int
    tiene_paquete: bool
    revision_vigente: int | None

    @property
    def categoria(self) -> CategoriaEstado:
        return _CATEGORIA[self.estado]

    def sobre(self, disponible: bool, motivo: MotivoNoDisponible | None) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "request_id": self.request_id, "estado": self.estado,
            "categoria_estado": self.categoria, "disponible": disponible, "motivo": motivo,
        }


def _estado_efectivo(estado_api: str | None, estado_flujo: str | None) -> str:
    """Una ejecución manual no tiene registro de la API: su estado sale del flujo."""
    if estado_api:
        return estado_api
    return estado_flujo if estado_flujo in _TERMINALES else "procesando"


def _resolver_run(con: sqlite3.Connection, run_id: str) -> _Run:
    sol = None
    if _tabla_existe(con, "solicitudes_ingesta"):
        sol = con.execute(
            "SELECT request_id, estado, recibido_en FROM solicitudes_ingesta WHERE run_id = ?;", (run_id,)
        ).fetchone()
    lote = con.execute(
        "SELECT request_id, origen_comunidad, periodo_referencia, total_recibidos, total_validos, "
        "total_rechazados, fecha_procesamiento, estado_flujo FROM lotes_procesamiento WHERE run_id = ?;",
        (run_id,),
    ).fetchone()
    if sol is None and lote is None:
        raise EjecucionNoEncontrada(f"No existe ninguna ejecución con run_id='{run_id}'.")
    revision = con.execute(
        "SELECT MAX(numero_revision) FROM paquetes_salida WHERE run_id = ?;", (run_id,)
    ).fetchone()[0]
    return _Run(
        run_id=run_id,
        request_id=(sol or lote)["request_id"],
        estado=_estado_efectivo(sol["estado"] if sol else None, lote["estado_flujo"] if lote else None),
        comunidad=lote["origen_comunidad"] if lote else None,
        periodo=lote["periodo_referencia"] if lote else None,
        fecha_recepcion=sol["recibido_en"] if sol else None,
        fecha_procesamiento=lote["fecha_procesamiento"] if lote else None,
        estado_flujo=lote["estado_flujo"] if lote else None,
        total_recibidos=lote["total_recibidos"] if lote else 0,
        total_validos=lote["total_validos"] if lote else 0,
        total_rechazados=lote["total_rechazados"] if lote else 0,
        tiene_paquete=revision is not None,
        revision_vigente=revision,
    )


def _disponibilidad(run: _Run, hay_datos: bool, sin_datos: MotivoNoDisponible) -> tuple[bool, MotivoNoDisponible | None]:
    if run.categoria == "en_curso":
        return False, "en_curso"
    return (True, None) if hay_datos else (False, sin_datos)


def _hay_analisis(con: sqlite3.Connection, run_id: str) -> bool:
    return con.execute(
        "SELECT 1 FROM mensajes_analizados WHERE run_id = ? LIMIT 1;", (run_id,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# 1. listar_ejecuciones
# ---------------------------------------------------------------------------
def _sql_ejecuciones(con: sqlite3.Connection) -> str:
    """Une los registros de la API con los lotes que no pasaron por ella (manuales)."""
    terminales = "(" + ", ".join(repr(t) for t in _TERMINALES) + ")"
    estado_lote = f"CASE WHEN l.estado_flujo IN {terminales} THEN l.estado_flujo ELSE 'procesando' END"
    desde_lotes = (
        "SELECT l.run_id, l.request_id, NULL AS fecha_recepcion, l.fecha_procesamiento, "
        f"l.origen_comunidad AS comunidad, l.periodo_referencia AS periodo, {estado_lote} AS estado "
        "FROM lotes_procesamiento l"
    )
    if not _tabla_existe(con, "solicitudes_ingesta"):
        return desde_lotes
    return (
        "SELECT s.run_id AS run_id, s.request_id AS request_id, s.recibido_en AS fecha_recepcion, "
        "l.fecha_procesamiento AS fecha_procesamiento, l.origen_comunidad AS comunidad, "
        "l.periodo_referencia AS periodo, s.estado AS estado "
        "FROM solicitudes_ingesta s LEFT JOIN lotes_procesamiento l ON l.run_id = s.run_id "
        f"UNION ALL {desde_lotes} "
        "WHERE NOT EXISTS (SELECT 1 FROM solicitudes_ingesta s WHERE s.run_id = l.run_id)"
    )


def listar_ejecuciones(
    limite: int = 20,
    cursor: str | None = None,
    estado: str | None = None,
    periodo: str | None = None,
    comunidad: str | None = None,
    db_path: str | Path | None = None,
) -> RespuestaListarEjecuciones:
    _validar_limite(limite, 100)
    _validar_opcion("estado", estado, ESTADOS)
    filtros: list[str] = []
    params: list[Any] = []
    for columna, valor in (("estado", estado), ("periodo", periodo), ("comunidad", comunidad)):
        if valor is not None:
            filtros.append(f"{columna} = ?")
            params.append(valor)
    clausula = f"WHERE {' AND '.join(filtros)}" if filtros else ""
    # Cursor por (fecha, run_id) y no por desplazamiento: los lotes nuevos que entran
    # durante la paginación no repiten ni saltan filas.
    pagina, params_pagina = clausula, list(params)
    if cursor is not None:
        orden, rid = _decodificar_cursor(cursor, 2)
        pagina += (" AND " if filtros else "WHERE ") + "(orden_fecha < ? OR (orden_fecha = ? AND run_id < ?))"
        params_pagina += [orden, orden, rid]

    with _lectura(db_path) as con:
        base = (
            "SELECT *, COALESCE(fecha_recepcion, fecha_procesamiento, '') AS orden_fecha, "
            "EXISTS (SELECT 1 FROM paquetes_salida p WHERE p.run_id = u.run_id) AS tiene_paquete "
            f"FROM ({_sql_ejecuciones(con)}) u"
        )
        total = con.execute(f"SELECT COUNT(*) FROM ({base}) {clausula};", params).fetchone()[0]
        filas = con.execute(
            f"SELECT * FROM ({base}) {pagina} ORDER BY orden_fecha DESC, run_id DESC LIMIT ?;",
            [*params_pagina, limite + 1],
        ).fetchall()

    hay_mas = len(filas) > limite
    filas = filas[:limite]
    items = [
        ItemEjecucionResumen(
            run_id=f["run_id"], request_id=f["request_id"], fecha_recepcion=f["fecha_recepcion"],
            fecha_procesamiento=f["fecha_procesamiento"], comunidad=f["comunidad"], periodo=f["periodo"],
            estado=f["estado"], categoria_estado=_CATEGORIA[f["estado"]], tiene_paquete=bool(f["tiene_paquete"]),
        )
        for f in filas
    ]
    siguiente = _codificar_cursor(filas[-1]["orden_fecha"], filas[-1]["run_id"]) if hay_mas else None
    return RespuestaListarEjecuciones(items=items, siguiente_cursor=siguiente, total=total)


# ---------------------------------------------------------------------------
# 2. obtener_ejecucion
# ---------------------------------------------------------------------------
def obtener_ejecucion(run_id: str, db_path: str | Path | None = None) -> RespuestaDetalleEjecucion:
    with _lectura(db_path) as con:
        run = _resolver_run(con, run_id)
    advertencia = None
    if run.estado == "completado" and not run.tiene_paquete:
        advertencia = "La ejecución figura como completada pero no tiene paquete de salida."
    return RespuestaDetalleEjecucion(
        run_id=run.run_id, request_id=run.request_id, comunidad=run.comunidad, periodo=run.periodo,
        fecha_recepcion=run.fecha_recepcion, fecha_procesamiento=run.fecha_procesamiento,
        estado=run.estado, categoria_estado=run.categoria, estado_flujo=run.estado_flujo,
        total_recibidos=run.total_recibidos, total_validos=run.total_validos,
        total_rechazados=run.total_rechazados, tiene_paquete=run.tiene_paquete,
        revision_vigente=run.revision_vigente, advertencia=advertencia,
    )


# ---------------------------------------------------------------------------
# 3. listar_mensajes
# ---------------------------------------------------------------------------
def _item_mensaje(f: sqlite3.Row, temas: list[str], entidades: list[str]) -> ItemMensajeAnalizadoUI:
    return ItemMensajeAnalizadoUI(
        source_id=f["source_id"],
        categoria_enrutamiento=f["categoria_enrutamiento"],
        sentimiento=f["sentimiento"],
        puntuacion=PuntuacionRelevancia(
            evidencia_explicita=f["evidencia_explicita"],
            utilidad_comunitaria=f["utilidad_comunitaria"],
            claridad_contexto=f["claridad_contexto"],
        ),
        motivo_seleccion=f["motivo_seleccion"],
        regla_aplicada=f["regla_aplicada"],
        temas=temas,
        entidades=entidades,
        requiere_soporte=bool(f["requiere_soporte"]),
        apto_para_publicacion=bool(f["apto_para_publicacion"]),
    )


def _hijos(con: sqlite3.Connection, tabla: str, columna: str, run_id: str, ids: list[str]) -> dict[str, list[str]]:
    resultado: dict[str, list[str]] = {i: [] for i in ids}
    if ids:
        filas = con.execute(
            f"SELECT source_id, {columna} FROM {tabla} WHERE run_id = ? AND source_id IN ({_en_lista(len(ids))}) "
            "ORDER BY id;",
            [run_id, *ids],
        ).fetchall()
        for f in filas:
            resultado[f[0]].append(f[1])
    return resultado


def listar_mensajes(
    run_id: str,
    apto: bool | None = None,
    categoria: str | None = None,
    sentimiento: str | None = None,
    requiere_soporte: bool | None = None,
    limite: int = 50,
    cursor: str | None = None,
    db_path: str | Path | None = None,
) -> RespuestaListarMensajes:
    _validar_limite(limite, 200)
    _validar_opcion("categoria", categoria, get_args(CategoriaEnrutamiento))
    _validar_opcion("sentimiento", sentimiento, get_args(Sentimiento))
    filtros = ["run_id = ?"]
    params: list[Any] = [run_id]
    for columna, valor in (
        ("apto_para_publicacion", apto), ("categoria_enrutamiento", categoria),
        ("sentimiento", sentimiento), ("requiere_soporte", requiere_soporte),
    ):
        if valor is not None:
            filtros.append(f"{columna} = ?")
            params.append(int(valor) if isinstance(valor, bool) else valor)
    donde = " AND ".join(filtros)
    donde_pagina, params_pagina = donde, list(params)
    if cursor is not None:
        (ultimo,) = _decodificar_cursor(cursor, 1)
        donde_pagina += " AND source_id > ?"
        params_pagina.append(ultimo)

    with _lectura(db_path) as con:
        run = _resolver_run(con, run_id)
        disponible, motivo = _disponibilidad(run, _hay_analisis(con, run_id), "sin_analisis")
        if not disponible:
            return RespuestaListarMensajes(**run.sobre(disponible, motivo))
        total = con.execute(f"SELECT COUNT(*) FROM mensajes_analizados WHERE {donde};", params).fetchone()[0]
        filas = con.execute(
            f"SELECT * FROM mensajes_analizados WHERE {donde_pagina} ORDER BY source_id LIMIT ?;",
            [*params_pagina, limite + 1],
        ).fetchall()
        hay_mas = len(filas) > limite
        filas = filas[:limite]
        ids = [f["source_id"] for f in filas]
        temas = _hijos(con, "mensaje_temas", "tema", run_id, ids)
        entidades = _hijos(con, "mensaje_entidades", "entidad", run_id, ids)
        items = [_item_mensaje(f, temas[f["source_id"]], entidades[f["source_id"]]) for f in filas]
    siguiente = _codificar_cursor(ids[-1]) if hay_mas else None
    return RespuestaListarMensajes(**run.sobre(True, None), items=items, siguiente_cursor=siguiente, total=total)


# ---------------------------------------------------------------------------
# 4. obtener_fuentes
# ---------------------------------------------------------------------------
def obtener_fuentes(
    run_id: str, source_ids: Sequence[str], db_path: str | Path | None = None
) -> RespuestaObtenerFuentes:
    ids = list(dict.fromkeys(s.strip() for s in source_ids if s and s.strip()))
    if not 1 <= len(ids) <= _MAX_FUENTES:
        raise ParametroInvalido(f"Debe especificar entre 1 y {_MAX_FUENTES} identificadores en 'source_ids'.")
    with _lectura(db_path) as con:
        run = _resolver_run(con, run_id)
        disponible, motivo = _disponibilidad(run, _hay_analisis(con, run_id), "sin_analisis")
        if not disponible:
            return RespuestaObtenerFuentes(**run.sobre(disponible, motivo), no_encontrados=ids)
        filas = con.execute(
            "SELECT source_id, autor, canal, tipo, texto_limpio FROM mensajes_analizados "
            f"WHERE run_id = ? AND source_id IN ({_en_lista(len(ids))});",
            [run_id, *ids],
        ).fetchall()
    por_id = {f["source_id"]: f for f in filas}
    items = [
        ItemFuenteOriginal(
            source_id=i, autor=por_id[i]["autor"], canal=por_id[i]["canal"],
            tipo=por_id[i]["tipo"], texto=por_id[i]["texto_limpio"],
        )
        for i in ids if i in por_id
    ]
    return RespuestaObtenerFuentes(
        **run.sobre(True, None), items=items, no_encontrados=[i for i in ids if i not in por_id]
    )


# ---------------------------------------------------------------------------
# 5. obtener_resumen
# ---------------------------------------------------------------------------
def obtener_resumen(run_id: str, db_path: str | Path | None = None) -> RespuestaObtenerResumen:
    with _lectura(db_path) as con:
        run = _resolver_run(con, run_id)
        fila = con.execute("SELECT * FROM resumen_comunidad WHERE run_id = ?;", (run_id,)).fetchone()
        disponible, motivo = _disponibilidad(run, fila is not None, "sin_analisis")
        if not disponible:
            return RespuestaObtenerResumen(**run.sobre(disponible, motivo))
        distribucion = {
            f[0]: f[1] for f in con.execute(
                "SELECT sentimiento, conteo FROM resumen_distribucion_sentimiento WHERE run_id = ? "
                "ORDER BY sentimiento;", (run_id,))
        }
        temas = [f[0] for f in con.execute(
            "SELECT tema FROM resumen_temas_principales WHERE run_id = ? ORDER BY orden;", (run_id,))]
        alertas = [f[0] for f in con.execute(
            "SELECT alerta FROM resumen_alertas_soporte WHERE run_id = ? ORDER BY id;", (run_id,))]
        resumen = ResumenComunidad(
            total_interacciones_procesadas=fila["total_interacciones_procesadas"],
            registros_validos=fila["registros_validos"],
            registros_rechazados=fila["registros_rechazados"],
            sentimiento_predominante=fila["sentimiento_predominante"],
            distribucion_sentimiento=distribucion, temas_principales=temas, alertas_soporte=alertas,
        )
    return RespuestaObtenerResumen(**run.sobre(True, None), resumen=resumen)


# ---------------------------------------------------------------------------
# 6. listar_rechazados
# ---------------------------------------------------------------------------
def listar_rechazados(
    run_id: str,
    origen: str | None = None,
    incluir_texto: bool = False,
    db_path: str | Path | None = None,
) -> RespuestaListarRechazados:
    _validar_opcion("origen", origen, _ORIGENES)
    with _lectura(db_path) as con:
        run = _resolver_run(con, run_id)
        disponible, motivo = _disponibilidad(run, True, "sin_analisis")
        if not disponible:
            return RespuestaListarRechazados(**run.sobre(disponible, motivo))
        # Consulta propia y no `leer_rechazados`: esta solo cubre los rechazos del validador.
        filas = con.execute(
            "SELECT mensaje_id, autor, canal, error_detalle, origen, fecha_aislamiento, texto_crudo "
            "FROM auditoria_rechazados WHERE run_id = ? AND (? IS NULL OR origen = ?) ORDER BY id;",
            (run_id, origen, origen),
        ).fetchall()
    items = [
        ItemRechazadoUI(
            mensaje_id=f["mensaje_id"], autor=f["autor"], canal=f["canal"], motivo=f["error_detalle"],
            origen=f["origen"], fecha_aislamiento=f["fecha_aislamiento"],
            texto_crudo=f["texto_crudo"] if incluir_texto else None,
        )
        for f in filas
    ]
    return RespuestaListarRechazados(**run.sobre(True, None), items=items, total=len(items))


# ---------------------------------------------------------------------------
# 7. listar_revisiones
# ---------------------------------------------------------------------------
def listar_revisiones(run_id: str, db_path: str | Path | None = None) -> RespuestaListarRevisiones:
    with _lectura(db_path) as con:
        run = _resolver_run(con, run_id)
        disponible, motivo = _disponibilidad(run, run.tiene_paquete, "sin_paquete")
        if not disponible:
            return RespuestaListarRevisiones(**run.sobre(disponible, motivo))
        filas = con.execute(
            "SELECT p.numero_revision, p.fecha_generacion, er.estado, er.revisor, er.comentarios, "
            "er.fecha_decision, eo.status_almacenamiento, eo.comprobacion_lectura "
            "FROM paquetes_salida p "
            "JOIN estado_revision er ON er.run_id = p.run_id AND er.numero_revision = p.numero_revision "
            "JOIN evidencia_almacenamiento_oci eo ON eo.run_id = p.run_id AND eo.numero_revision = p.numero_revision "
            "WHERE p.run_id = ? ORDER BY p.numero_revision DESC;",
            (run_id,),
        ).fetchall()
    items = [
        ItemRevisionResumen(
            numero_revision=f["numero_revision"], estado=f["estado"], revisor=f["revisor"],
            comentarios=f["comentarios"], fecha_decision=f["fecha_decision"],
            fecha_generacion=f["fecha_generacion"], status_almacenamiento=f["status_almacenamiento"],
            comprobacion_lectura=bool(f["comprobacion_lectura"]),
        )
        for f in filas
    ]
    return RespuestaListarRevisiones(**run.sobre(True, None), revision_vigente=run.revision_vigente, items=items)


# ---------------------------------------------------------------------------
# 8. obtener_revision
# ---------------------------------------------------------------------------
def obtener_revision(
    run_id: str, numero_revision: int, db_path: str | Path | None = None
) -> RespuestaObtenerRevision:
    with _lectura(db_path) as con:
        _resolver_run(con, run_id)
        existe = con.execute(
            "SELECT 1 FROM paquetes_salida WHERE run_id = ? AND numero_revision = ?;", (run_id, numero_revision)
        ).fetchone()
    if existe is None:
        raise RevisionNoEncontrada(f"No existe la revisión {numero_revision} de run_id='{run_id}'.")
    try:
        paquete = PaqueteSalida.model_validate(
            reconstruir_json_paquete_salida(run_id, numero_revision, db_path=db_path)
        )
    except (sqlite3.Error, ValueError) as exc:  # ValueError: la base no cumple el Contrato 3
        logger.error("lectura_fallida", run_id=run_id, revision=numero_revision, error=str(exc)[:300])
        raise LecturaFallida(f"No se pudo reconstruir la revisión {numero_revision}.") from exc
    return RespuestaObtenerRevision(
        run_id=run_id, numero_revision=numero_revision, paquete=paquete,
        evidencia_oci=paquete.almacenamiento_oci,
    )
