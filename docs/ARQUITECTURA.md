# Arquitectura de moooney

Diagramas en Mermaid: GitHub los renderiza al abrir este fichero. Para verlos en local, cualquier
editor con vista previa de Markdown y Mermaid (VS Code con la extensión "Markdown Preview Mermaid
Support") o [mermaid.live](https://mermaid.live).

## 1. Flujo de datos de extremo a extremo

Qué entra, por qué etapas pasa y dónde se guarda cada cosa. Las etapas en gris son las fases
pendientes (3, 4 y 5).

```mermaid
flowchart LR
    subgraph entrada["Entrada manual"]
        XLSX[("data/excel/*.xlsx<br/>exportación Santander")]
    end

    subgraph f1["Fase 1 · Ingesta (sin IA)"]
        DETECT["detect.py<br/>SHA-256 del fichero"]
        PARSE["parse_santander.py<br/>cabecera, alias de columnas,<br/>importes es-ES, IBAN → ****NNNN"]
        LOAD["load.py<br/>huella sha1(cuenta|fecha|importe|concepto|saldo)"]
        CSV[("data/clean/*.csv")]
    end

    subgraph f2["Fase 2 · Categorización"]
        NORM["normalize.py<br/>concepto → clave de comercio"]
        CASCADE["pipeline.py<br/>reglas → embeddings → Clef"]
        REVIEW["review<br/>corrección manual (source=user)"]
    end

    subgraph f25["Fase 2.5 · Enriquecimiento (opcional)"]
        QUERY["query.py<br/>clave limpia, sin región,<br/>sin personas"]
        PROV["providers.py<br/>Nominatim → SearXNG → DuckDuckGo → Google Places"]
        ROUND2["runner.py<br/>segunda ronda con Clef<br/>solo si mejora la confianza"]
    end

    subgraph f3["Fases 3-5 · pendientes"]
        METRICS["analytics/<br/>métricas, recurrentes,<br/>presupuestos, alertas"]:::todo
        REPORT["report/<br/>previsión e informe mensual<br/>(Qwen sobre agregados)"]:::todo
        WATCH["watch / tarea programada / UI"]:::todo
    end

    DB[("data/moooney.db<br/>SQLite")]

    XLSX --> DETECT --> PARSE --> LOAD
    PARSE --> CSV
    LOAD --> DB
    DB --> NORM --> CASCADE --> DB
    DB --> REVIEW --> DB
    CASCADE -. "confianza < 0,6<br/>o categoría Otros" .-> QUERY --> PROV --> ROUND2 --> DB
    DB --> METRICS --> REPORT
    WATCH -.-> DETECT

    classDef todo fill:#eee,stroke:#999,color:#666,stroke-dasharray: 4 3
```

## 2. Cascada de categorización

Cada **clave de comercio** (no cada transacción) baja por esta cascada una sola vez. El
resultado se guarda en la tabla `merchants` con su fuente, y la fuente manda al recategorizar:
`user > rule > embedding > clef_web > clef > llm`.

```mermaid
flowchart TD
    KEY["Clave de comercio nueva<br/>(p. ej. CAFETERIA GATO AZUL)"]
    MEM{"¿Ya está en<br/>merchants?"}
    RULES{"¿Casa con una regla<br/>de config/rules.yaml?"}
    EMB{"¿Embedding bge-m3 similar (≥ 0,92)<br/>a una clave user/rule?"}
    CLEF["Clef-Flash<br/>state: clave + importe medio redondeado<br/>+ nº apariciones + intervalo<br/>preguntas: category, flow_type, is_recurring"]
    CONF{"¿confianza ≥ 0,6<br/>y no es Otros?"}
    ENR{"¿enriquecimiento<br/>activado?"}
    WEB["Consulta externa<br/>→ resumen + tipo de lugar<br/>→ Clef otra vez"]
    SAVE[("merchants + propagación<br/>a transactions")]
    REV["Cola de review<br/>(corrección manual)"]

    KEY --> MEM
    MEM -- sí --> SAVE
    MEM -- no --> RULES
    RULES -- "sí · source=rule · conf 1,0" --> SAVE
    RULES -- no --> EMB
    EMB -- "sí · source=embedding" --> SAVE
    EMB -- no --> CLEF --> CONF
    CONF -- "sí · source=clef" --> SAVE
    CONF -- no --> ENR
    ENR -- no --> REV
    ENR -- sí --> WEB
    WEB -- "mejora · source=clef_web" --> SAVE
    WEB -- "no mejora" --> REV
    REV -- "source=user · conf 1,0" --> SAVE
```

## 3. Fronteras de privacidad: qué sale de dónde

El principio del proyecto es que los datos bancarios no salen de la máquina. Este diagrama
marca las tres zonas y exactamente qué cruza cada frontera.

```mermaid
flowchart LR
    subgraph local["Tu máquina (Windows)"]
        direction TB
        RAW[("Excel, CSV limpio, SQLite<br/>conceptos, importes, saldos, IBAN")]
        ANON["llm/anonymize.py<br/>rechaza IBAN, 8+ dígitos,<br/>grupos tipo tarjeta, saldos"]
        LOG1[("llm_calls<br/>modelo, propósito, tokens, hash")]
        LOG2[("web_calls<br/>proveedor, consulta, estado")]
    end

    subgraph org["ClusterTIC · proxy LiteLLM (autoalojado)"]
        CLEF["clef-flash<br/>decisiones"]
        BGE["bge-m3<br/>embeddings"]
        QWEN["qwen3.8-flash-next<br/>informes (fases 4+)"]
    end

    subgraph internet["Internet (solo fase 2.5, opcional, desactivable)"]
        NOM["Nominatim (OSM)"]
        DDG["DuckDuckGo"]
        GP["Google Places<br/>(solo con clave)"]
    end

    RAW -- "claves de comercio +<br/>importe medio redondeado a decenas<br/>+ nº apariciones" --> ANON
    ANON --> CLEF
    ANON --> BGE
    RAW -. "agregados mensuales<br/>por categoría (fase 4)" .-> QWEN
    ANON -- "solo la clave de comercio limpia<br/>(sin importes, fechas, personas)" --> NOM
    ANON --> DDG
    ANON --> GP
    CLEF --> LOG1
    BGE --> LOG1
    NOM --> LOG2
    DDG --> LOG2
    GP --> LOG2

    style internet fill:#fff3e0,stroke:#e65100
    style org fill:#e8f5e9,stroke:#2e7d32
    style local fill:#e3f2fd,stroke:#1565c0
```

Lo que **nunca** cruza ninguna frontera: el IBAN completo, saldos, importes exactos, fechas de
movimientos individuales, conceptos crudos y nombres de personas. `MOOONEY_OFFLINE=1` bloquea
las dos fronteras a la vez.

## 4. Módulos y tablas

```mermaid
flowchart TB
    subgraph cli["cli.py (typer)"]
        C1[inspect] --- C2[ingest] --- C3[status] --- C4[merchants] --- C5[categorize] --- C6[enrich] --- C7[review] --- C8[eval-clef]
    end

    subgraph src["src/moooney/"]
        ING["ingest/<br/>detect · parse_santander · load"]
        CAT["categorize/<br/>normalize · keys · rules · taxonomy<br/>embed · pipeline · evaluate"]
        ENRM["enrich/<br/>settings · query · providers · runner"]
        LLM["llm/<br/>client · anonymize · decide"]
        DBM["db/<br/>schema.sql · repo"]
    end

    subgraph tablas["SQLite · data/moooney.db"]
        T1[(imports)]
        T2[(transactions)]
        T3[(merchants)]
        T4[(merchant_embeddings)]
        T5[(merchant_enrichment)]
        T6[(llm_calls)]
        T7[(web_calls)]
        T8[(budgets · alerts<br/>fase 3)]:::todo
    end

    subgraph cfg["config/*.yaml"]
        Y1[settings]
        Y2[categories]
        Y3[rules]
        Y4[llm]
        Y5[enrichment]
    end

    cli --> ING & CAT & ENRM
    CAT --> LLM
    ENRM --> LLM
    ING & CAT & ENRM & LLM --> DBM --> tablas
    Y1 --> ING
    Y2 & Y3 --> CAT
    Y4 --> LLM
    Y5 --> ENRM

    classDef todo fill:#eee,stroke:#999,color:#666,stroke-dasharray: 4 3
```
