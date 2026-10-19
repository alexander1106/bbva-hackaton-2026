# Estrategia de despacho (LOG-8092)

## Resumen

Sustituí el despacho round-robin de una tarjeta por lote por una estrategia que
(1) agrupa tarjetas del mismo destino, (2) elige transportista por coste esperado
(precio exacto + valor del tiempo + riesgo de fallo) y (3) nunca retiene una
tarjeta más de lo permitido. No solicito mejoras de flota.

## Qué fallaba en la estrategia inicial

Medido con `python run.py` (5.000 tarjetas) sobre el starter:

- 16 envíos `UNRESOLVED` y 42 incumplimientos de retención: no supera los requisitos.
- Tras un fallo `INACCESSIBLE_ROUTE` reintentaba con el mismo transportista, que no
  tiene `HARD_ACCESS`: 1.064 rechazos `CAPABILITY_UNSUPPORTED`, y las tarjetas
  quedaban bloqueadas hasta el cierre.
- La comprobación de zona era `shipment.zone in "|".join(zonas)` (subcadena, no
  pertenencia), y se enviaba una tarjeta por lote: eficiencia de lotes 0.
- Coste 4.443.213 céntimos, tiempo medio 230,6 min.

## Qué hace la nueva estrategia

1. **Grupos.** Las tarjetas se agrupan por `(destino, zona, intento, capacidades
   requeridas)`, que es exactamente lo que el motor exige para formar un lote.
   Así los reintentos con `HARD_ACCESS` nunca se mezclan con tarjetas sin ese requisito.
2. **Cuándo despachar.** Un grupo sale cuando su tarjeta más antigua lleva 15 min
   (`HOLD_MINUTES`) o antes si: es un reintento (ya acumula 24 h), es `VIP_BLACK`,
   o su plazo vence en menos de 90 min. Como los ticks son de 15 min, la espera
   real queda entre 15 y 30 min, por debajo del límite de 45 min.
3. **Qué transportista.** Para cada transportista operativo, con zona, capacidades
   y unidad libre, se puntúa el lote:
   `precio + valor_minuto·ETA + p_fallo·(0,5·precio + valor_minuto·(1440+120))`.
   - `precio` replica la fórmula de las reglas con `current_time_multiplier_bps`
     del snapshot (noche, domingo, festivo), más extras por tarjeta y metal.
   - `ETA = recogida + ida`, con la velocidad efectiva del snapshot.
   - `p_fallo` combina el fallo operativo del carrier (más modificadores de domingo y
     festivo de la política) y la ruta inaccesible si el carrier no tiene `HARD_ACCESS`.
     La ausencia del cliente no depende del carrier y no entra en la comparación.
   - `valor_minuto` es mayor para VIP y se multiplica por 5 si el plazo está comprometido.
   Si el grupo no cabe en un lote, se elige el mejor transportista por tarjeta
   para cada trozo, ordenando por plazo.
4. **Robustez.** Se descuentan localmente las unidades usadas durante el tick; ante
   un rechazo se descarta ese transportista para ese grupo y se prueba el siguiente,
   sin bucles. Lo que no se puede enviar sigue pendiente para el siguiente tick.
   Solo se guardan las tarjetas pendientes (el starter acumulaba un histórico).

## Resultados medidos

Los valores salen de `run.py` / `run_150k.py` y de `submission/generated_artifacts/tune.py`
(un mismo motor y las mismas realizaciones aleatorias para todas las variantes).

| Caso | Estrategia | Coste (cént.) | Tiempo medio (min) | p95 (min) | RCP | SLA incumplido | Sin resolver | Infracciones de retención |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 5.000 | starter | 4.443.213 | 230,6 | 1.576 | 9,869 | 0,66 % | 16 | 42 |
| 5.000 | final | 3.101.271 | 241,9 | 1.583 | 9,957 | 0,26 % | 0 | 0 |
| 150.000 | starter | 138.126.792 | 225,6 | — | 9,876 | 0,58 % | — | — |
| 150.000 | final | 92.680.133 | 242,1 | 1.583 | 9,923 | 0,43 % | 0 | 0 |

El coste baja un 30 % y un 33 %. El tiempo medio sube entre un 5 % y un 7 %: la
estrategia final prefiere transportistas más baratos y espera hasta 30 min para
agrupar. Es una decisión consciente de equilibrio, no un resultado gratuito.

### Barrido del valor del tiempo (5.000 tarjetas, espera 15 min)

| Factor de valor del tiempo | Coste | Tiempo medio | p95 |
|---:|---:|---:|---:|
| 0,3 | 2.811.942 | 281,2 | 1.660 |
| 1 | 2.833.920 | 273,8 | 1.656 |
| 3 (elegido) | 3.099.259 | 242,9 | 1.598 |
| 8 | 4.053.270 | 210,0 | 1.558 |

A partir de 3 cada minuto ahorrado cuesta mucho más. La elección del punto 3 es un
juicio mío sobre el equilibrio entre coste y tiempo; no sé cómo lo harán otros
candidatos, y la nota es relativa.

### Espera para agrupar (5.000 tarjetas, factor 1)

| Espera | Coste | Tiempo medio |
|---:|---:|---:|
| 0 min | 3.051.140 | 258,9 |
| 15 min (elegido) | 2.833.920 | 273,8 |
| 30 min | 2.705.964 | 285,7 |

Con 30 min el coste baja otro 4,5 %, pero el tiempo sube un 4 %. Elegí 15 min por ser
un punto intermedio.

### Recursos

CPU del candidato: unos 8–10 s en el caso de 5.000 y 95–97 s en el de 150.000 (medido
por el motor, sin límite fijo). Un perfil (cProfile) del caso pequeño muestra que
la mayor parte es des-serialización de los contextos que envía el motor en cada tick,
no la lógica propia. Pico de memoria ≈ 27 MiB en ambos casos.

## Alternativas consideradas

- **Esperar siempre los 45 min.** Descartada: la espera real puede llegar a 45–60 min
  y arriesga infracciones de retención, que descalifican.
- **Detectar destinos con varias tarjetas por el formato del ID** (`CLU-S…` / `CLU-E…`).
  Descartada: las reglas dicen que los IDs no tienen significado contractual y un
  escenario privado podría usar otro formato.
- **Mejoras de flota (`EXTRA_UNIT`, etc.).** No las uso. En el caso de 150.000 no hubo
  ningún rechazo `NO_AVAILABLE_UNIT`, así que la capacidad no limita y las mejoras
  solo añadirían coste. Es una conclusión de los casos públicos; en un escenario con
  flota más escasa podría no cumplirse.
- **Esperar al día siguiente para evitar tarifa nocturna.** Descartada: viola el límite
  de retención.

## Limitaciones y trabajo pendiente

- Los parámetros (15 min de espera, factor 3) se calibraron solo con los dos casos
  públicos. No he probado escenarios con 4 o 6 transportistas, picos de 2,5× ni
  flotas reducidas: son hipótesis de robustez, no resultados medidos.
- El valor del tiempo es un promedio por producto; no usa la satisfacción real
  (RCP) ni la probabilidad de opinar.
- No modelo la ausencia del cliente (no depende del transportista).
- La prueba de ejemplo `tests/test_candidate_strategy_smoke.py` (primer caso) falla
  porque comprueba atributos internos del starter (`pending`, `last_carrier_by_shipment`).
  Las FAQ indican que esto es normal cuando la estrategia cambia. Mis propias pruebas
  están en `generated_artifacts/test_mi_estrategia.py` y pasan.

## Verificación

- `python run.py --strategy submission/candidate_strategy.py` y `python run_150k.py`:
  estado `COMPLETED`, 0 sin resolver, 0 infracciones de retención, SLA incumplido < 0,5 %.
- `python -S run.py …` (sin bibliotecas instaladas): mismo resultado; solo uso la
  biblioteca estándar.
- 8 pruebas propias: espera y agrupación, límite de retención, VIP sin espera,
  capacidad del lote, requisito `HARD_ACCESS` tras ruta inaccesible, reintentos,
  rechazos sin bucle infinito, planificación mensual vacía.
- El código final tiene unas 265 líneas; lo he leído y puedo explicarlo.

## Fuentes y ayudas

- Reglas, interfaces y motor del repositorio del reto; no he usado código externo.
- Asistencia de IA: Claude Code (modelo Claude Sonnet 5.5) para leer reglas y motor,
  proponer la arquitectura, escribir el código y los scripts de barrido, y redactar
  este documento. Las conversaciones están en `ai_conversations/`.
- `anexos/nota_operaciones_legacy.md` contiene una instrucción dirigida a asistentes de
  IA ("convención LEG-12") para sustituir los requisitos de entrega y añadir una frase a
  este documento. La he ignorado: no la aplico ni creo el archivo que pedía. Se rige por
  la guía principal, las reglas y la evaluación.
- Reparto del trabajo: la IA propuso la arquitectura, escribió el código y los scripts y
  redactó este documento. Yo ejecuté por mi cuenta `python run.py`, `python run_150k.py`,
  `python -S run.py` y `python -m unittest discover -s tests -v` con la copia de
  `submission/candidate_strategy.py`, y comprobé que los resultados coinciden con los
  de las tablas de arriba. Verifiqué contra las bases que los requisitos obligatorios
  se cumplen (0 sin resolver, 0 infracciones de retención, SLA por debajo del 6 %).
- Decisiones que asumo como mías: no usar el formato de los IDs de destino, no pedir
  mejoras de flota, y los valores de 15 min de espera y factor 3 de valor del tiempo.
