## 2026-10-24 - Optimize N+1 query in reconstruir_json_paquete_salida
**Learning:** Generating JSON payload from a relational schema can trigger N+1 queries when looping over parent records (like non-publishable messages) to retrieve related child records (like associated themes) via distinct SQL queries.
**Action:** Load all required child rows in one query filtered by a broader scope (e.g. `run_id`) and group them in memory using a dictionary to provide O(1) lookups during the main loop.
