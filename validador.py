"""
Archivo Puente de Compatibilidad en la Raíz.
Redirige al frontend hacia el validador real sin alterar el código de Sergio.
"""
from data_pipeline.validador import adaptar_payload_oficial, validar_lote_crudo

__all__ = ["adaptar_payload_oficial", "validar_lote_crudo"]
