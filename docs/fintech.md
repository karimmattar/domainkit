# Fintech: a bank's first review, answered

A bank, a payment institution, or their auditors read a new service's
stack against a short list before anything else: its threat model, where
its releases come from, where its keys live, who checks whom, whether its
audit trail can be trusted and names the people behind it, what a retried
request does, and whether a dump of the database shows a card number.
This page answers each item with the page that explains it, the setting
or command that turns it on, and the test that proves it, named as
`file::test`. `domainkit_tests/test_fintech_docs.py` checks that every
test named here exists.

## A project with the controls on

```bash
domainkit init bankAPI --profile fintech
cd bankAPI
python manage.py makemigrations -m "fintech"
python manage.py migrate
pytest
```

`--profile fintech` writes the project `init` always writes, with every
control below installed, configured, and wired
([ADR 0124](adr/0124-write-a-banks-controls-on-and-wired-as-a-profile-over-the-project-template.md)):

| What | Where in the project |
| --- | --- |
| PCI DSS v4.0's passwords, second factors, step-up, and transactions authorized for one amount and payee | `AUTH` in `config/settings/base.py`; the `mfa`, `stepup`, and `sca` routers in `config/urls.py` |
| The audit trail, sealed by the worker, anchored on the ledger where deployed, append-only in PostgreSQL after every `migrate`, kept a year at least (PCI DSS v4.0 10.5.1) | `domainkit.contrib.audit`, whose triggers `migrate` makes; `AUDIT` in `production.py`, with `MIN_RETENTION` of 365 days, which the PCI preset holds too |
| Evidence on immudb | `domainkit.contrib.evidence`; `LEDGERS` and `EVIDENCE` in `production.py` |
| Idempotency keys, required where money moves | `IdempotencyMiddleware`, last in `MIDDLEWARE`; the transfer route refuses a request without one (`400 idempotency_key_required`) |
| Messaging: the outbox, its inbox, ordering keys, and the worker that runs approved commands | `domainkit.contrib.messaging` |
| Encrypted fields under AWS KMS or Vault; the development key only in development and tests | `KEYS` in `base.py` (`local`) and `production.py` (`KEYS_PROVIDER`: `aws-kms` or `vault`) |
| Double-entry books, postings, captures, and reversals over a threshold approved by two clerks; a currency the thresholds do not name always needs approval | `domainkit.contrib.accounting`; `APPROVALS.ENTRY_THRESHOLDS`. The sample transfer posts nothing to them: book a project's movements there in its own use case |
| Maker-checker, with a step-up to approve | `domainkit.contrib.approvals`; `APPROVALS.APPROVE_RULE = "config.rules.RECENT_PROOF"` (`StepUp(max_age=timedelta(minutes=5))`) |
| Limits per customer, per transaction and per day, in each currency transfers are made in (USD and EUR); a movement in a currency no limit names is refused | `domainkit.contrib.limits`; `LIMITS.DEFAULTS`; `CURRENCIES` in `domains/payments/domain/model.py` |
| Export and erasure, which destroys a person's keys, held while they have an account with money on it, and keeping the books and the evidence of who approved what, pseudonymised | `domainkit.contrib.privacy`; `on_erase_check` |
| Deny by default | every route names its rule; `check` lists any that does not, and staging and production refuse to start |
| A sample transfer through the customer's controls | `domains/payments`: a card registered after a step-up, its number sealed with a blind index; a transfer in `Money`, in USD or EUR only, confirmed by the maker for its amount and card, sent with an `Idempotency-Key`, counted against the maker's limit in its own transaction, and over 1,000.00 submitted for another person's approval (`202`); with its tests in `tests/`. It books nothing in the double-entry books and records no evidence: the books and evidence are installed for the project's own use cases |

The production settings read their secrets from the environment, and the
docstring of `config/settings/production.py` lists each: the database,
Redis, `SECRET_KEY`, `EVIDENCE_SUBJECT_KEY`, the key service and the data
key it wrapped (`manage.py keys generate` prints it), and the ledger's URL,
CA, and signing key. Once they are set, `manage.py check --deploy` passes
with no error and no warning; without one, the project does not start and
names the variable. The project's CI runs ruff, `mypy --strict`, `check`,
its migrations, and its tests.

## The first-review list

| Item | The page | The setting or command | The test |
| --- | --- | --- | --- |
| Threat model | [threat-model.md](threat-model.md#moving-money): trust boundaries by STRIDE, and a transfer's flow through the controls | Read with the project's own threat model, which knows its data and deployment | `domainkit_tests/test_fintech_docs.py::test_the_threat_model_covers_moving_money`, `domainkit_tests/test_docs.py::test_every_link_leads_somewhere` |
| Provenance | [security.md](security.md#verifying-a-release); [SECURITY.md](../SECURITY.md) | `gh attestation verify domainkit-<version>-py3-none-any.whl --repo karimmattar/domainkit`: SLSA build provenance for the wheel and the sdist, and a CycloneDX bill of materials | `domainkit_tests/test_ci.py::test_the_run_that_builds_the_files_attests_them`, `domainkit_tests/test_ci.py::test_releases_publish_only_attested_files`, `domainkit_tests/test_ci.py::test_the_bill_of_materials_is_one_attest_takes` |
| KMS | [contrib/keys.md](contrib/keys.md#data-keys-and-the-kek) | `KEYS.PROVIDER = "aws-kms"` or `"vault"`, `DATA_KEYS`; `manage.py keys generate`, `rewrap`, `reencrypt`; `keys.E001` refuses the development key where deployed | `domainkit_tests/test_keys_providers.py::test_kms_makes_unwraps_and_rewraps_data_keys`, `domainkit_tests/test_keys_providers.py::test_vault_makes_unwraps_and_rewraps_data_keys`, `domainkit_tests/test_keys_app.py::test_a_deployment_on_the_local_kek_must_opt_in`, `domainkit_tests/test_init_fintech.py::test_production_refuses_the_development_key_service` |
| Maker-checker with step-up | [contrib/approvals.md](contrib/approvals.md#who-may-approve-step-up); [auth/step-up.md](auth/step-up.md) | `@requires_approval(...)` and `guard(...)`; `APPROVALS.APPROVE_RULE` naming a `StepUp` | `domainkit_tests/test_approvals.py::test_the_maker_never_decides_and_no_one_decides_twice`, `domainkit_tests/test_approvals.py::test_twenty_approvals_at_once_count_to_ten_and_run_once`, `domainkit_tests/test_approvals.py::test_what_runs_is_what_was_approved`, `domainkit_tests/test_approvals_api.py::test_a_policy_asks_its_approvers_to_step_up`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_transfers.py-tpl::test_a_large_transfer_runs_once_another_person_approves_it` |
| Verifiable audit with operator identity | [contrib/audit.md](contrib/audit.md#tamper-evidence), [In staging and production](contrib/audit.md#in-staging-and-production); [operators](reference/commands.md#operators) | `AUDIT.LEDGER`, `AUDIT.NAMESPACE`, sealing by the worker, the `protect()` triggers; `manage.py auditseal`, `auditverify`. A request's actor is its authenticated principal; a command's operator is the name it is given (`--operator`, `DOMAINKIT_OPERATOR`, or the login name), which nothing verifies: who may run commands where deployed is the bank's | `domainkit_tests/test_audit_seals.py::test_auditverify_names_each_problem`, `domainkit_tests/test_audit_baseline.py::test_startup_refuses_with_require_baseline`, `domainkit_tests/test_operators.py::test_commands_that_change_data_or_credentials_name_their_operator`, `domainkit_tests/test_audit15_fin_90_statements_unaudited.py::test_importing_and_reconciling_over_http_are_in_the_trail`, `domainkit_tests/test_audit_retention.py::test_the_pci_preset_keeps_the_trail_a_year_at_least`, `domainkit_tests/test_init_fintech.py::test_migrate_makes_the_trail_append_only_on_postgresql`, `src/domainkit/management/templates/profiles/fintech/tests/test_controls.py-tpl::test_the_trail_names_who_moved_money_and_verifies`, `src/domainkit/management/templates/profiles/fintech/tests/test_controls.py-tpl::test_operators_changes_name_the_operator` |
| Written idempotency semantics | [contrib/idempotency.md](contrib/idempotency.md): what each retry gets, situation by situation | `IdempotencyMiddleware`; `IDEMPOTENCY.TTL_SECONDS`, `TOMBSTONE_SECONDS`, `LEASE_SECONDS` | `domainkit_tests/test_ratelimit_and_idempotency.py::test_retries_replay_the_first_response`, `domainkit_tests/test_ratelimit_and_idempotency.py::test_key_misuse_is_refused`, `domainkit_tests/test_ratelimit_and_idempotency.py::test_committed_requests_never_run_twice`, `domainkit_tests/test_idempotency_tombstones.py::test_a_committed_key_is_not_run_again_once_its_ttl_passes`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_transfers.py-tpl::test_a_small_transfer_is_made_once_however_often_it_is_sent`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_transfers.py-tpl::test_a_transfer_needs_an_idempotency_key` |
| A card-number column unreadable in a dump | [contrib/keys.md](contrib/keys.md#what-the-database-a-dump-and-a-backup-hold) | `Annotated[str \| None, Encrypted(subject=..., blind_index=True)]`; `dumpdata` writes ciphertext, and `--decrypt` is audited | `domainkit_tests/test_encrypted_fields.py::test_a_card_number_is_unreadable_in_the_database`, `domainkit_tests/test_encrypted_fields.py::test_dumpdata_writes_ciphertext_unless_asked_to_decrypt`, `domainkit_tests/test_keys_app.py::test_a_decrypted_dump_is_recorded_with_its_tables`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_cards.py-tpl::test_the_card_number_is_unreadable_in_the_database_and_a_dump` |

## The fintech audit's other items

The audit read domainkit against what a payments or ledger product needs
([audit/0.9.0.md](audit/0.9.0.md#the-fintech-audit)). These are answered:

| Id | Item | The page | The setting or command | The test |
| --- | --- | --- | --- | --- |
| FIN-01 | Money: an exact amount and its currency | [money.md](money.md) | `domainkit.money.Money`; a field hinted `Money` | `domainkit_tests/test_money.py::test_amounts_hold_exactly_their_currencys_places`, `domainkit_tests/test_money_db.py::test_money_round_trips_exactly` |
| FIN-02 | Named rounding | [money.md](money.md) | `Rounding`, `multiply(..., rounding=...)` | `domainkit_tests/test_money.py::test_multiply_is_exact_or_rounds_as_named` |
| FIN-03 | Allocation that loses no cent | [money.md](money.md) | `amount.allocate(ratios)` | `domainkit_tests/test_money.py::test_allocation_never_loses_or_creates_a_minor_unit` |
| FIN-07 | Double-entry books, append-only, corrected by reversal; no account overdrawn unless opened so; what each entry moved in the audit trail | [contrib/accounting.md](contrib/accounting.md) | `post_entry`, `reverse_entry`; `ACCOUNTING.REQUIRE_PROTECTION`; `may_go_negative` | `domainkit_tests/test_accounting.py::test_concurrent_transfers_keep_every_entry_balanced_and_lose_no_update`, `domainkit_tests/test_accounting_ledger.py::test_entries_are_append_only_on_postgresql`, `domainkit_tests/test_accounting_ledger.py::test_a_reversal_undoes_an_entry_once`, `domainkit_tests/test_accounting_models.py::test_an_account_may_not_go_negative_by_default`, `domainkit_tests/test_accounting_models.py::test_an_entry_balances_per_currency` |
| FIN-08 | Holds and available balances | [contrib/accounting.md](contrib/accounting.md) | `place_hold`, `capture_hold` | `domainkit_tests/test_accounting_ledger.py::test_a_capture_posts_its_entry_and_releases_the_rest` |
| FIN-10 | Periods, closed in date order and for good; trial balance, general ledger | [contrib/accounting.md](contrib/accounting.md#periods) | `manage.py accounting trialbalance`, `ledger` | `domainkit_tests/test_accounting_ledger.py::test_no_entry_is_dated_in_a_closed_period`, `domainkit_tests/test_accounting_ledger.py::test_periods_close_in_date_order`, `domainkit_tests/test_audit15_fin_87_closed_period_changed.py::test_a_period_opened_before_a_closed_one_leaves_it_as_reported`, `domainkit_tests/test_audit15_fin_87_closed_period_changed.py::test_an_entry_outside_every_period_leaves_a_closed_one_as_reported`, `domainkit_tests/test_accounting_reports.py::test_the_general_ledger_is_the_same_bytes_every_time` |
| FIN-11 | Reconciliation against statements | [contrib/accounting.md](contrib/accounting.md) | `manage.py accounting import`, `reconcile`; `ACCOUNTING.MATCHING_RULES` | `domainkit_tests/test_accounting_reconciliation.py::test_reconciliation_matches_and_records_breaks` |
| FIN-18 | Exactly-once effects: an inbox | [contrib/messaging.md](contrib/messaging.md) | `@inbox` | `domainkit_tests/test_messaging_inbox.py::test_a_message_run_again_after_its_commit_applies_its_effect_once` |
| FIN-19 | Ordering per key | [contrib/messaging.md](contrib/messaging.md) | `ordering_key=`, `@ordered(...)` | `domainkit_tests/test_messaging_ordering.py::test_a_keys_messages_run_one_at_a_time_in_commit_order_across_workers` |
| FIN-23 | Field encryption under a KMS | [contrib/keys.md](contrib/keys.md) | `Encrypted()`; `KEYS` | as KMS and the card-number column above |
| FIN-24 | Crypto-shredding; erasure held while the law keeps the record | [contrib/keys.md](contrib/keys.md#erasing-a-person-crypto-shredding), [privacy.md](privacy.md#what-the-law-keeps-holding-an-erasure) | `Encrypted(subject=...)`; `manage.py privacy erase`; `on_erase_check` | `domainkit_tests/test_privacy.py::test_a_check_holds_the_whole_erasure_before_anything_is_erased`, `domainkit_tests/test_keys_app.py::test_erasing_a_person_destroys_their_keys`, `domainkit_tests/test_encrypted_fields.py::test_a_shredded_subjects_values_read_as_none`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_cards.py-tpl::test_erasing_the_owner_shreds_their_card_number` |
| FIN-29 | Strong customer authentication with dynamic linking | [auth/step-up.md](auth/step-up.md#transactions-authorized-for-one-amount-and-payee) | `request_authorization`, `consume_authorization`; `AUTH.SCA` | `domainkit_tests/test_auth_sca.py::test_any_change_of_amount_or_payee_voids_it`, `domainkit_tests/test_auth_sca.py::test_one_of_many_concurrent_uses_wins`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_transfers.py-tpl::test_an_authorization_holds_for_its_amount_and_card_only` |
| FIN-30 | Step-up | [auth/step-up.md](auth/step-up.md) | `StepUp(max_age=...)`; `AUTH.STEP_UP` | `domainkit_tests/test_auth_stepup.py::test_a_login_token_steps_up_for_max_age`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_cards.py-tpl::test_registering_a_card_asks_for_a_step_up` |
| FIN-36 | Maker-checker, reversals included, and only while the maker may still make the command | [contrib/approvals.md](contrib/approvals.md) | as above | as maker-checker above, and `domainkit_tests/test_audit15_fin_88_reversal_unapproved.py::test_reversing_an_entry_over_the_threshold_needs_its_approval`, `domainkit_tests/test_audit15_fin_93_inactive_maker_runs.py::test_a_request_of_a_maker_deactivated_since_never_runs`, `domainkit_tests/test_audit15_fin_89_unlisted_currency_approvals.py::test_a_million_pounds_needs_approval_where_a_thousand_dollars_does` |
| FIN-38 | Secrets and key rotation | [contrib/keys.md](contrib/keys.md#rotating); [contrib/evidence.md](contrib/evidence.md) | `manage.py keys rewrap`, `reencrypt`; `EVIDENCE.SUBJECT_KEYS`; `SECRET_KEY_FALLBACKS` | `domainkit_tests/test_keys_app.py::test_rotating_the_data_key_and_reencrypting`, `domainkit_tests/test_keys_providers.py::test_rotating_secret_key_keeps_old_data_keys_unwrapping`, `domainkit_tests/test_evidence_subject_keys.py::test_a_new_subject_key_writes_new_keys_and_still_finds_old_records` |
| FIN-42 | PCI passwords, lockout, throttling | [auth/pci-dss.md](auth/pci-dss.md) | `AUTH.PASSWORD_PRESET = "pci-dss-4"` | `domainkit_tests/test_auth_password_preset.py::test_the_preset_sets_what_is_left_unset`, `domainkit_tests/test_auth_password_preset.py::test_settings_that_contradict_the_preset_are_refused`, `src/domainkit/management/templates/profiles/fintech/tests/test_controls.py-tpl::test_passwords_follow_pci_dss` |
| FIN-47 | Transaction and velocity limits, failing closed for a currency they do not name | [contrib/limits.md](contrib/limits.md) | `LIMITS.DEFAULTS`, `consume`; `manage.py limits set` | `domainkit_tests/test_audit15_fin_89_unlisted_currency_limits.py::test_a_holders_payout_in_another_currency_is_still_limited`, `domainkit_tests/test_limits_earlier.py::test_a_limit_set_at_eleven_counts_every_holders_morning`, `domainkit_tests/test_limits.py::test_fifty_payments_at_once_pass_exactly_what_the_limit_allows`, `domainkit_tests/test_limits_usage.py::test_a_limit_for_one_subject_replaces_the_one_for_every_subject`, `src/domainkit/management/templates/profiles/fintech/tests/payments/test_transfers.py-tpl::test_a_transfer_over_the_customers_limit_moves_nothing` |
| FIN-74 | A bank's first review, answered by a page and a test each | this page | `domainkit init --profile fintech`; `manage.py check --deploy` | `domainkit_tests/test_fintech_docs.py::test_every_test_the_fintech_page_names_exists`, `domainkit_tests/test_init_fintech.py::test_a_fintech_project_passes_its_ci_workflow`, `domainkit_tests/test_init_fintech.py::test_production_passes_check_deploy_once_its_secrets_are_set` |
| FIN-77 | Lost updates on tables without `version` | [databases.md](databases.md#versions) | a `version` on every aggregate table | `domainkit_tests/test_aggregate_versions.py::test_an_aggregate_table_without_a_version_is_refused` |
| FIN-78 | Retries, isolation, and locks | [databases.md](databases.md#running-work-again) | `uow.run(..., retry=Retry(...))`, `Isolation.SERIALIZABLE`, `get_many_for_update` | `domainkit_tests/test_transactions.py::test_work_lost_to_a_race_runs_again_whole` |
| FIN-79 | Idempotency leases on the database's clock, above the request timeout | [contrib/idempotency.md](contrib/idempotency.md) | `IDEMPOTENCY.LEASE_SECONDS`, `REQUIRE_DEADLINE` | `domainkit_tests/test_ratelimit_and_idempotency.py::test_a_host_running_ahead_never_takes_a_running_requests_key`, `domainkit_tests/test_ratelimit_and_idempotency.py::test_keyed_requests_without_any_deadline_are_refused_where_deployed` |
| FIN-80 | Handlers run once: the inbox | [contrib/messaging.md](contrib/messaging.md) | `@inbox` | `domainkit_tests/test_messaging_inbox.py::test_a_handler_blocked_past_its_lease_commits_once` |
| FIN-84 | A committed key is never run again within its tombstone | [contrib/idempotency.md](contrib/idempotency.md) | `IDEMPOTENCY.TOMBSTONE_SECONDS` | `domainkit_tests/test_idempotency_tombstones.py::test_a_committed_key_is_not_run_again_once_its_ttl_passes` |
| FIN-85 | SBOM, SLSA provenance, threat model | as provenance and the threat model above | | as above |
| N4 | Ledger trust that a reconnect or another process keeps | [databases.md](databases.md#ledgers-immudb) | `LEDGERS` with `TLS_CA` and `SERVER_SIGNING_KEY` | `domainkit_tests/test_ledger_trust.py::test_a_forked_ledger_is_tampering`, `domainkit_tests/test_ledger_trust.py::test_a_new_process_starts_from_the_saved_state` |
| N5 | Tamper evidence required where deployed | [contrib/audit.md](contrib/audit.md#in-staging-and-production) | `AUDIT.REQUIRE_BASELINE` | `domainkit_tests/test_audit_baseline.py::test_startup_refuses_with_require_baseline` |
| N6 | Retention that keeps the chain verifiable | [contrib/audit.md](contrib/audit.md#retention-that-keeps-the-chain) | `manage.py auditprune`; `AUDIT.CHECKPOINT_KEY` | `domainkit_tests/test_audit_checkpoints.py::test_a_pruned_chain_still_verifies` |
| N8 | Operators named and audited | [operators](reference/commands.md#operators) | `--operator`, `DOMAINKIT_OPERATOR` | `domainkit_tests/test_operators.py::test_commands_run_as_their_operator` |
| N11 | Audit anchors namespaced per project | [contrib/audit.md](contrib/audit.md) | `AUDIT.NAMESPACE` | `domainkit_tests/test_audit_seals.py::test_anchor_keys_carry_the_namespace` |

## What stays the bank's

- Running the key service, immudb, PostgreSQL, and Redis, with their
  credentials and certificates; rotating the KEK in its service.
- Its own threat model, and the limits, thresholds, and approvers its
  risk appetite asks for: the profile's are examples
  (`LIMITS.DEFAULTS`, `APPROVAL_THRESHOLD` in `domains/payments/apps.py`,
  `APPROVALS`).
- Enrolling users in a second factor, and keeping the trail, the ledger,
  and backups as long as its regulator asks: the profile keeps the trail
  a year, the PCI DSS floor; raise `AUDIT.MIN_RETENTION` to the
  regulator's period
  ([threat-model.md](threat-model.md#what-a-project-must-do-itself)).
- Booking its own movements in the double-entry books, and recording
  their evidence: the sample transfer does neither. A use case that
  posts its entry under its own guard passes the ledger's thresholds
  when its policy asks at least `ENTRY_APPROVALS` approvals.
- Who may run `manage.py` commands where deployed: an operator names
  themselves, and nothing verifies the name.
