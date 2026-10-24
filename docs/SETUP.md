# Configuración del entorno — CommunityLab (Hackathon ONE G10)

Este proyecto usa **uv** como administrador de paquetes Python. Con `pyproject.toml` y `.python-version` ya fijados, `uv` instala automáticamente Python 3.11 si no lo tienen, y resuelve las mismas versiones de dependencias en Windows, macOS y Linux.

## 1. Instalar uv (una sola vez por máquina)

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verifiquen con `uv --version`.

## 2. Clonar el repo y sincronizar el entorno

```bash
git clone <url-del-repo>
cd communitylab
uv sync
```

`uv sync` lee `pyproject.toml` y `.python-version`, descarga Python 3.11 si hace falta, crea el `.venv` local y instala las dependencias exactas.

## 3. Generar y compartir el lockfile (lo hace la primera persona, una sola vez)

```bash
uv lock
git add uv.lock
git commit -m "Fijar versiones exactas de dependencias"
git push
```

A partir de ahí, cada integrante que haga `git pull` + `uv sync` obtiene **exactamente las mismas versiones**, sin importar su sistema operativo. Si alguien necesita agregar una dependencia nueva, usa `uv add <paquete>` (esto actualiza `pyproject.toml` y `uv.lock` automáticamente) en vez de `pip install`.

En CI (o para verificar que nadie olvidó actualizar el lock) usen `uv sync --locked`, que falla si `uv.lock` no coincide con `pyproject.toml`.

### Cooldown de 7 días ante paquetes recién publicados

`pyproject.toml` define `exclude-newer = "7 days"`: al resolver dependencias (`uv lock`, `uv add`), uv ignora versiones publicadas hace menos de 7 días. Es una defensa contra ataques a la cadena de suministro (releases maliciosos que PyPI aún no ha retirado). Implicaciones:

- **Requiere uv >= 0.11** (`required-version` ya lo exige). Si `uv --version` muestra algo anterior, actualicen con `uv self update`.
- Solo aplica al **resolver**; las versiones ya fijadas en `uv.lock` no cambian.
- Si una dependencia nueva "no existe" al agregarla, probablemente salió hace menos de 7 días: esperen, o exceptúenla temporalmente con `exclude-newer-package = { nombre = false }` en `[tool.uv]` (y retiren la excepción después).
- Para parches de seguridad urgentes, usen esa misma excepción puntual, no desactiven el cooldown global.

## 4. Ejecutar los servicios

```bash
# Backend (FastAPI)
uv run uvicorn app.main:app --reload

# Interfaz de curaduría (Streamlit)
uv run streamlit run app/streamlit_app.py
```

## 5. Notebooks / celdas interactivas (opcional, solo desarrollo)

`ipykernel` está en el grupo `dev`, así que `uv sync` ya lo instala. En VS Code, seleccionen como kernel el intérprete `.venv` del proyecto. Para un uso puntual sin tocar el lock: `uv run --with ipykernel ...`.

**Cuidado:** los `.ipynb` guardan las salidas de las celdas, que pueden incluir mensajes reales de la comunidad o claves de API. No suban notebooks con salidas: límpienlas antes del commit (p. ej. `uvx nbstripout archivo.ipynb`) o agreguen `*.ipynb` al `.gitignore`.

## Notas

- **Proveedor de LLM:** aún no definido en el equipo. PydanticAI permite cambiar de modelo sin tocar la arquitectura del agente, así que no bloquea el avance del resto del stack.
- **`oci` (SDK de Oracle):** ya validado como compatible con Python 3.11 en Windows, macOS y Linux (ver README del hackathon, requisito obligatorio de OCI Object Storage).
- Si Windows Defender o el antivirus corporativo bloquea el script de instalación de PowerShell, pueden instalar uv vía `pip install uv` como alternativa (menos recomendable, pero funciona).
