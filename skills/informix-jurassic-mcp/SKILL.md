---
name: informix-jurassic-mcp
description: "Consulta SIEMPRE el MCP jurassic-mcp antes de responder o escribir código sobre las bases de datos Informix (ERP SEMIC, Genero 4GL, semicsa y bases de clientes). Úsala cuando se mencione Informix, una base de datos, tablas, columnas, campos, índices, claves, SQL, una consulta/SELECT/JOIN, un informe o listado, código 4GL/Genero con SQL embebido, o entidades de negocio del ERP (clientes, artículos, albaranes, facturas, pedidos, tarifas, stock, almacenes, contabilidad, RRHH, tickets, contratos…), o nombres crípticos de tabla/columna (albcab, cod_cli, n_albven…). Nunca inventes nombres de tablas o columnas: compruébalos con las herramientas del MCP."
---

# Informix vía jurassic-mcp

El esquema real de las bases Informix lo conoce el MCP **jurassic-mcp**, no tú. Los nombres son heredados y crípticos (`albcab`, `cod_cli`, `n_albven`, `imp_pe`…) y cambian entre bases. Por eso hay que **comprobarlo siempre con el MCP** antes de afirmar nada del esquema, escribir SQL o tocar código que acceda a la base.

## Regla principal

- **Nunca supongas** que una tabla o columna existe, ni su tipo o su significado. Búscala y descríbela con el MCP.
- Usa el MCP **sin pedir permiso** para las herramientas de lectura. Son baratas (~0,5–1 s cada una) y no tocan datos.
- Si el MCP no responde o no está conectado, **dilo y para**: no sigas inventando el esquema. Indica al usuario cómo arrancarlo (ver «Si el MCP no está disponible»).

## Herramientas

Con el servidor registrado como `jurassic-mcp`, los nombres completos son `mcp__jurassic-mcp__<herramienta>`.

| Herramienta | Cuándo |
|---|---|
| `list_databases` | No sabes en qué base está algo. Las descripciones dicen qué contiene cada una |
| `search_schema(database, pattern)` | **Primer paso** para cualquier concepto («albarán», «cliente», `cod_art`). Busca en nombres **y** en las descripciones documentadas |
| `list_tables(database, name_filter?)` | Ver tablas por prefijo (`alb%`, `fac%`) |
| `describe_table(database, table)` | Columnas, tipos reales (`DECIMAL(12,2)`, `DATETIME YEAR TO SECOND`), nulabilidad y descripciones |
| `list_indexes` / `list_foreign_keys` | Claves para los JOIN. En estas bases casi no hay FKs declaradas: deduce la relación por los índices únicos y los nombres de columna compartidos (`empresa`, `cod_ser`, `n_albven`…) |
| `validate_sql(database, sql)` | **Obligatorio** antes de entregar un SELECT. Solo hace PREPARE en el servidor (no ejecuta): comprueba sintaxis, tablas, columnas y permisos |
| `discovery_overview` | Ver qué tablas son las grandes y cuáles están documentadas |
| `save_descriptions` | Guardar el significado de tablas o columnas que el usuario **haya confirmado** |

## Flujo de trabajo

1. **Base de datos.** Si no está clara, usa `list_databases` y elige por su descripción. Si hay varias candidatas, pregunta.
2. **Localiza.** Usa `search_schema` con el término de negocio y sus abreviaturas habituales. Ejemplos:
   - cliente → `cli`
   - albarán → `alb`
   - factura → `fac`
   - pedido → `ped`
   - artículo → `art`
   - almacén → `alm`
   - importe → `imp`
   - fecha → `fec`
   - serie → `ser`
   - ejercicio → `eje`
   - número → `n_`
   - cabecera / línea → `cab` / `lin`

   Prueba varias si la primera no da resultados.
3. **Entiende.** Usa `describe_table` en cada tabla que vayas a usar, y `list_indexes` para conocer sus claves. Apóyate en las `description` que devuelve, que las ha escrito el equipo.
4. **Escribe el SQL** en dialecto Informix, usando solo columnas que hayas visto en `describe_table`.
5. **Valida** con `validate_sql`. Si da error, corrígelo y vuelve a validar hasta que salga `valid: true`.
6. **Responde** citando las tablas y columnas usadas. Si alguna relación es deducida y no está declarada, dilo.

## Dialecto Informix (errores típicos)

- **Límite de filas:** `SELECT FIRST 10 …` o `SELECT SKIP 20 FIRST 10 …`. No existen `LIMIT` ni `TOP`.
- **Fechas:**
  - `TODAY` y `CURRENT` (un datetime, p. ej. `CURRENT YEAR TO SECOND`);
  - `MDY(m,d,y)`, `YEAR(f)`, `MONTH(f)`, `DAY(f)`;
  - intervalos: `f - 7 UNITS DAY`.
- **Literales de fecha:** dependen de `DBDATE` / `GL_DATE`. Usa `MDY()` para no depender de ellos.
- **Texto:**
  - concatenación con `||`;
  - las columnas `CHAR` vienen rellenas con espacios, así que usa `TRIM()` al comparar o mostrar;
  - patrones con `MATCHES '*x*'` o `LIKE '%x%'`.
- **Nulos y condicionales:** `NVL(x, 0)`, `DECODE(...)` y `CASE WHEN`.
- **Outer join:** usa `LEFT OUTER JOIN … ON …` (ANSI) y no mezcles con la sintaxis antigua `OUTER tabla` en la misma consulta.
- **Clave empresa:** casi todas las tablas del ERP llevan `empresa` en la clave, así que inclúyela en los JOIN y en los filtros.
- **`WITH` (CTE):** solo existe en Informix 14.10 o superior. Si `validate_sql` lo rechaza, usa subconsultas.
- **Tablas temporales:** `SELECT … INTO TEMP` crea tablas; el MCP no lo valida. Úsalo solo en código 4GL, nunca en consultas para el usuario.

## Código 4GL / Genero

- Antes de modificar o escribir SQL embebido (`DECLARE … CURSOR FOR`, `SELECT … INTO`, `PREPARE`) o un `DEFINE … LIKE tabla.columna`, comprueba las tablas y columnas con `describe_table`.
- Para validar con el MCP el SQL de un `SELECT … INTO variables`, quita el `INTO variables` (el MCP rechaza cualquier `INTO`). Los parámetros `?` se pueden dejar tal cual, porque PREPARE los acepta. Las variables `:var` o `$var`, en cambio, sustitúyelas por `?`.

## Aprender del usuario

- Cuando el usuario explique qué significa una tabla, una columna o un código, ofrécete a guardarlo con `save_descriptions`. Hazlo solo si lo confirma, nunca con suposiciones tuyas.
- Si una base está casi sin documentar y el trabajo va a ser largo, sugiere el prompt `/mcp__jurassic-mcp__autodiscover <base>`.

## Límites actuales

- **No se pueden consultar datos.** El MCP ve el esquema, pero no ejecuta consultas. No afirmes resultados («hay 120 albaranes»): entrega el SQL validado para que lo ejecute el usuario.
- **Recuentos de filas:** `discovery_overview` da recuentos por tabla, que sí son datos fiables.

## Si el MCP no está disponible

Si las herramientas `mcp__jurassic-mcp__*` no aparecen o fallan al conectar:

1. Arrancarlo desde el repo jurassic-mcp: `uv run jurassic-mcp --http`, o F5 en VS Code.
2. Registrarlo una vez en Claude Code: `claude mcp add --transport http jurassic-mcp http://127.0.0.1:8000/mcp`.
3. Si el error es de conexión a Informix (SQLCODE -908, -951…), muestra el mensaje y la pista que devuelve el MCP. No intentes adivinar el esquema mientras tanto.
