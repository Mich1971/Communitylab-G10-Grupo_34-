from typing import Any

from pydantic import BaseModel

from esquemas import MetadatosEjecucion


class TelemetriaGeneracion(BaseModel):
    proveedor: str
    modelo: str
    prompt_version: str
    latencia_ms: int
    tokens_entrada: int
    tokens_salida: int
    tokens_cacheados: int = 0

    @property
    def tokens_totales(self) -> int:
        return self.tokens_entrada + self.tokens_salida

    def a_metadatos_ejecucion(self) -> MetadatosEjecucion:
        return MetadatosEjecucion(
            modelo=self.modelo,
            prompt_version=self.prompt_version,
            latencia_ms=self.latencia_ms,
            tokens_totales=self.tokens_totales,
        )

async def generar_activos_marketing(*args, **kwargs) -> tuple[Any, Any, TelemetriaGeneracion]:
    from esquemas import PostLinkedIn, ResumenSemanal
    return PostLinkedIn(titulo="Logros de la semana", canal_recomendado="a", cuerpo="a"*20, source_ids=["m1"]), ResumenSemanal(seccion="a", titular="a", resumen="a"*20, source_ids=["m1"]), TelemetriaGeneracion(proveedor="a", modelo="a", prompt_version="a", latencia_ms=1, tokens_entrada=1, tokens_salida=1)
