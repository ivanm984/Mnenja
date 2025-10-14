# Mnenja

## Konfiguracija podatkovne baze

Aplikacija sedaj uporablja izključno MySQL ali PostgreSQL podatkovne baze. Povezavo lahko
nastavite z neposrednim `DATABASE_URL` (npr. `mysql://user:pass@host:3306/mnenja` ali
`postgresql://user:pass@host:5432/mnenja`) ali pa z okoljem definirate posamezne spremenljivke:

- **MySQL**: `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`
- **PostgreSQL**: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DATABASE`

Ob zagonu se shema avtomatsko inicializira in vključuje tudi tabelo `generated_reports`, kjer se
shranjujejo potrjena poročila/mnenja skupaj s pripadajočo dokumentacijo.

## Migracija iz obstoječega SQLite arhiva

Za selitev starih podatkov iz lokalne SQLite baze uporabite ukaz:

```bash
python migrate_sqlite.py --sqlite-path ./local_sessions.db
```

Če ciljni DSN ni definiran prek okolja, ga lahko podate neposredno:

```bash
python migrate_sqlite.py --sqlite-path ./local_sessions.db --database-url postgresql://user:pass@localhost:5432/mnenja
```

Skript preslika vse shranjene seje in revizije v novo bazo ter o uspehu izpiše statistiko prenosa.
