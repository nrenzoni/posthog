-- Hobby skips the partition-table replacement that originally introduced this arbiter.
-- Node person writes require ON CONFLICT (team_id, uuid) on the actual posthog_person.
-- Keep this transactional: CREATE INDEX takes a SHARE lock and blocks writes until commit.
-- Run with ingestion/workers quiesced during the guarded maintenance migration. Large hobby
-- tables may require a long maintenance window. Duplicates or a conflicting index name abort
-- the migration and its tracking insert; this migration never repairs or deletes person data.
DO $migration$
DECLARE
    person_table regclass := 'posthog_person'::regclass;
    person_schema text;
    person_name text;
    index_name constant text := 'posthog_person_team_uuid_upsert_idx';
    usable_arbiter boolean;
    deferred_arbiter boolean;
BEGIN
    -- Match arbiter semantics, not historical names or column order. INCLUDE columns do
    -- not participate in uniqueness, so only inspect the first indnkeyatts attributes.
    SELECT bool_or(idx.indisvalid AND idx.indisready AND idx.indimmediate),
           bool_or(idx.indisvalid AND idx.indisready AND NOT idx.indimmediate)
      INTO usable_arbiter, deferred_arbiter
        FROM pg_index AS idx
        JOIN pg_attribute AS team_column
          ON team_column.attrelid = idx.indrelid
         AND team_column.attname = 'team_id'
         AND NOT team_column.attisdropped
        JOIN pg_attribute AS uuid_column
          ON uuid_column.attrelid = idx.indrelid
         AND uuid_column.attname = 'uuid'
         AND NOT uuid_column.attisdropped
        WHERE idx.indrelid = person_table
          AND idx.indisunique
          AND idx.indpred IS NULL
          AND idx.indexprs IS NULL
          AND idx.indnkeyatts = 2
          AND (
              (idx.indkey[0] = team_column.attnum AND idx.indkey[1] = uuid_column.attnum)
              OR (idx.indkey[0] = uuid_column.attnum AND idx.indkey[1] = team_column.attnum)
          );

    -- A deferrable matching constraint can still be inferred by ON CONFLICT even if we
    -- add an immediate index. Do not claim to have repaired that incompatible schema.
    IF deferred_arbiter THEN
        RAISE EXCEPTION 'Cannot ensure person upsert arbiter: a matching deferrable unique constraint requires explicit schema maintenance';
    END IF;

    IF usable_arbiter THEN
        RETURN;
    END IF;

    SELECT namespace.nspname, relation.relname
      INTO STRICT person_schema, person_name
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
     WHERE relation.oid = person_table;

    -- Do not use IF NOT EXISTS: it would silently accept an invalid or unrelated index.
    IF to_regclass(format('%I.%I', person_schema, index_name)) IS NOT NULL THEN
        RAISE EXCEPTION 'Cannot ensure person upsert arbiter: %.% already exists without an equivalent valid immediate unique (team_id, uuid) index',
            person_schema, index_name;
    END IF;

    EXECUTE format(
        'CREATE UNIQUE INDEX %I ON %I.%I (team_id, uuid)',
        index_name, person_schema, person_name
    );
END
$migration$;
