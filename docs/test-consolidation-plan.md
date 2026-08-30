# Test consolidation plan

**Baseline**: `uv run --with pytest pytest --collect-only -q` → **715 tests collected**
(696 across the 73 files analyzed below, 19 in the 5 files excluded from this pass:
`test_background_use.py`, `test_computer_use.py`, `test_background_delivery.py`,
`test_spatial.py`, `test_world_model.py` — a separate fork is actively editing those,
they weren't read here to avoid conflicting with that work. Fold them into a
`test_computer_use.py` target (see below) once that fork lands, they already have a
reasonable subject-based name and likely need no further split.

**Method**: every one of the 73 non-excluded files' test function names were read in
full (696 function names, not file names alone) and grouped by what the function
actually asserts, inferred from its name and, for ambiguous ones, its body. Most
"round"/"review" files are grab-bags spanning many real subjects, not single-topic —
splitting them is most of the work here, not just renaming.

**Target**: 73 files → **31 subject files** (roughly 60% fewer), plus the 5
already-reasonably-named files left alone (`test_providers.py`, `test_desktop_window.py`,
`test_slash_registry.py`, `test_webui_projects.py`, `test_onboarding.py`) for **31 + 5 = 36
total**, once `test_computer_use.py` (renamed from `test_computer_use.py`/merged with the
other 4 excluded files) is added back in.

**Safety rule for execution**: after every merge, re-run
`pytest --collect-only -q` and diff the collected test IDs (not just the count) against
the baseline list below. A matching count with a different test disappearing while a
duplicate appears is a real bug this plan must catch, not paper over.

---

## Clean already, no change needed

- `test_providers.py`
- `test_desktop_window.py`
- `test_slash_registry.py`
- `test_webui_projects.py`
- `test_onboarding.py`

## Excluded from this pass (separate fork mid-edit)

- `test_background_use.py`, `test_computer_use.py`, `test_background_delivery.py`,
  `test_spatial.py`, `test_world_model.py` → once that lands, evaluate whether these
  five should merge into one `test_computer_use.py` (they're all real subject-matter
  already, just possibly over-split — a smaller follow-up decision, not blocking the
  rest of this plan).

---

## New target files and what moves into each

### `test_safety.py`
Full files: `test_auth_scopes.py`, `test_autonomous_mode.py`, `test_guardrails.py`,
`test_safety_features.py`, `test_safety_matrix.py`, `test_security_hardening.py`,
`test_security_review_r1.py`, `test_security_review_r2.py`.
Plus, pulled out of grab-bag files:
- `test_product_hardening.py`: `test_safety_hardline_vs_recoverable`,
  `test_safety_ask_requires_approver_and_is_fail_closed`, `test_safety_scope_and_deny_mode`
- `test_production_hardening.py`: `test_sandboxed_skips_guardrail_ask_but_ask_mode_hardline_holds`,
  `test_sandboxed_deny_mode_still_denies_guardrail`, `test_reserved_scope_still_asks_when_sandboxed`,
  `test_python_tool_parity_with_shell_guardrails`
- `test_review_regressions.py`: `test_safety_blocks_no_preserve_root_bypass`
- `test_round1_features.py`: `test_allow_rule_prefix_matches_pointer_signature`,
  `test_desktop_prefix_rules_cannot_bypass_deny_or_hardline_paths`,
  `test_webapprover_persists_action_signature_for_desktop_tools`

### `test_trust_and_network.py`
Pulled from `test_security_hardening.py` (splits from the safety bucket above — these
are trust/env/SSRF, a distinct real subject from command-safety):
`test_ssrf_*` (7 tests), `test_trust_*` (4 tests), `test_load_env_*` (2 tests),
`test_mcp_config_gated_by_trust`, `test_project_hooks_gated_by_trust`,
`test_playwright_filters_followup_requests`, `test_playwright_route_pins_validated_address`,
`test_pinned_browser_proxy_connects_only_to_recorded_address`,
`test_gateway_cli_requires_allowlist`, `test_gateway_allow_parsing`, `test_gateway_redact_token`,
`test_serve_token_enforced`, `test_serve_rejects_evil_host_and_origin`,
`test_webui_host_origin_cookie_guards`, `test_save_data_urls_sanitizes_sid`.
Plus `test_security_review_r1.py`'s `test_serve_warns_when_auth_disabled` (move here,
not `test_safety.py`, since it's auth not command-safety).

### `test_audit_provenance.py`
Full file: `test_audit_chain.py`.
Plus: `test_round1_features.py`'s `test_stamp_record_adds_provenance_and_commits_content`,
`test_visible_footer_only_in_visible_mode_and_idempotent`,
`test_config_provenance_marking_validation_and_env`, `test_eval_runner_stamps_saved_trajectory`,
`test_cli_run_json_out_stamps_and_visible_footer`; `test_round3_wiring.py`'s
`test_provenance_footer_reaches_webui_done_event`.

### `test_context_and_compaction.py`
Full files: `test_context.py`, `test_context_parity.py`, `test_context_hermes.py`.
Plus: `test_audit_fixes.py`'s `test_compact_noop_on_short_history`,
`test_compact_force_noop_on_short_history`, `test_invariants_merge_text_and_vision_user_messages`,
`test_run_survives_empty_response_after_vision_message`, `test_tool_call_cap_keeps_history_well_formed`,
`test_estimate_message_tokens_ignores_base64_bulk`,
`test_run_with_resume_and_attachments_appends_vision_message`;
`test_review_regressions.py`'s `test_compact_never_orphans_tool_results`,
`test_compact_force_boundary_also_safe`, `test_stream_retry_does_not_duplicate_deltas`;
`test_production_hardening.py`'s `test_token_meter_calibrates_and_projects`,
`test_token_meter_ignores_zero_usage`, `test_loop_uses_calibration_for_compaction_threshold`,
`test_estimate_tokens_cjk_counts_per_char`, `test_compact_pinned_summary_does_not_duplicate_excerpt`,
`test_compact_fallback_summary_pins_digest_once`;
`test_round4_perf.py`'s `test_estimate_tokens_hot_path_uses_precompiled_regex`;
`test_round3_upgrades.py`'s `test_compaction_fallback_emits_structured_sections`,
`test_config_defaults_raised_for_long_horizon_tasks`;
`test_round2_bugfixes.py`'s `test_estimate_message_tokens_counts_tool_call_arguments`,
`test_compaction_triggers_sooner_with_heavy_tool_calls`.

### `test_agent_loop.py`
Full files: `test_loop.py`, `test_messages.py`, `test_bare_tool_json.py`, `test_live_wire.py`,
`test_truncation_continue.py`, `test_alignment.py`.
Plus: `test_audit2_fixes.py`'s `test_provider_env_resolved_lazily`, `test_model_switch_rebuilds_client`.

### `test_mcp.py`
Pulled from `test_mcp_and_durable.py`: `test_mcp_handshake_and_list`,
`test_mcp_call_roundtrip_ok_and_error`, `test_mcp_proxy_through_registry`,
`test_mcp_unreachable_server_warns_not_raises`.
Plus `test_review_fixes.py`'s `test_review9_mcp_collision_aliases_instead_of_crash`;
`test_round2_regressions.py`'s `test_mcp_timeout_frees_lock_and_recovers`;
`test_live_fixes.py`'s `test_mcp_config_accepts_wrapper_and_flat_shapes`,
`test_mcp_config_reports_invalid_entries`; `test_parity_round2.py`'s
`test_mcp_http_client_parses_json_and_sse`.

### `test_checkpoints_sessions.py`
Full file: `test_stateful_checkpoints.py`.
Plus: `test_mcp_and_durable.py`'s `test_checkpoint_each_step_and_crash_resume`,
`test_agent_facade_auto_checkpoints` (the non-MCP half of that file);
`test_production_hardening.py`'s `test_list_sessions_uncapped_by_default`,
`test_search_finds_needle_in_oldest_session`,
`test_hydrate_falls_back_to_checkpoint_for_interrupted_runs`,
`test_read_meta_after_appends_first_line_only`, `test_chain_head_cache_keeps_chain_valid_under_load`,
`test_chain_head_cache_invalidated_by_external_edit`, `test_ephemeral_store_writes_nothing`,
`test_subagent_isolated_store_and_shared_approver`, `test_try_begin_run_is_atomic_and_generations_monotonic`,
`test_request_stop_only_flags_until_finish`;
`test_round4_perf.py`'s `test_session_meta_cache_skips_re_reads`,
`test_set_project_invalidates_meta_cache`, `test_metadata_updates_do_not_rewrite_transcript`,
`test_session_writers_share_lock_across_store_instances`;
`test_live_fixes.py`'s `test_long_session_ids_do_not_collide`;
`test_review_round3.py`'s `test_approval_ids_are_session_namespaced`,
`test_hydrate_parses_error_bodies_with_retry_hint`;
`test_review_fixes.py`'s `test_session_create_unique_under_collision`,
`test_review3_session_id_passthrough`.

### `test_webui_core.py`
Full file: `test_webui.py` (already clean, just renamed for consistency with the other
`test_webui_*` files).

### `test_webui_frontend.py`
Full files: `test_webui_e2e.py`, `test_webui_newui.py`, `test_frontend_wiring.py`,
`test_competitive_ui.py`.
Plus: `test_round3_wiring.py`'s `test_settings_controls_exist_in_index_html`,
`test_app_js_wires_the_new_controls`, `test_served_assets_match_disk_and_carry_controls`,
`test_metrics_endpoint_served_with_auth` (this one could also fit `test_usage_metrics.py`,
pick one, don't duplicate).

### `test_settings.py`
Full files: `test_settings_audit.py`, `test_settings_fields.py`, `test_settings_panel.py`.

### `test_file_tools.py`
Full files: `test_tools.py`, `test_tool_toggles.py`.
Plus: `test_functionality_review_r1.py`'s `test_glob_skips_matches_outside_workspace`,
`test_grep_skips_matches_outside_workspace`, `test_glob_still_finds_nested_files`;
`test_round3_upgrades.py`'s `test_edit_file_exact_match_survives_crlf_files`,
`test_edit_file_fuzzy_fallback_indentation`, `test_edit_file_still_fails_clean_when_unfindable`,
`test_edit_file_ambiguous_exact_still_rejected`, `test_render_file_diff_works_with_fuzzy_match`,
`test_grep_skips_binary_files`, `test_grep_ignore_case_flag`;
`test_review_round_fixes.py`'s `test_write_file_journals_creations_so_revert_can_undo_them`,
`test_flexible_match_preserves_line_boundaries`, `test_flexible_match_blank_lines_and_uniqueness`,
`test_edit_file_rejects_empty_old_string_cleanly`, `test_render_file_diff_mirrors_edit_file_rules`,
`test_gates_preview_against_workspace_root_not_cwd`,
`test_compaction_files_section_lists_only_mutations`;
`test_round1_features.py`'s `test_write_file_external_verify_success_and_failure`,
`test_syntax_error_skips_external_verify`;
`test_v06_features.py`'s `test_write_valid_python_has_no_warning`,
`test_write_broken_python_warns_with_line`, `test_non_python_files_not_checked`,
`test_edit_introducing_syntax_error_warns`, `test_edit_fixing_syntax_clears_warning`;
`test_competitive_parity.py`'s `test_write_and_edit_are_journaled_and_revertible`,
`test_restore_creates_tombstone_for_formerly_absent_file`.
Also security-adjacent path checks that belong here not in `test_safety.py` (they're about
file-tool correctness, not command-safety): `test_security_review_r1.py`'s
`test_write_file_refuses_all_state_files`, `test_edit_file_refuses_all_state_files`,
`test_nested_and_dotdot_privileged_paths_refused`, `test_benign_writes_still_allowed`,
`test_revert_refuses_privileged_target`, `test_rewind_refuses_privileged_target`,
`test_revert_still_restores_normal_files`; `test_security_hardening.py`'s
`test_write_file_refuses_privileged_paths`, `test_edit_file_refuses_privileged_paths`,
`test_write_file_refuses_symlink_to_privileged_path`, `test_privileged_path_normalization`.

### `test_repo_index_lsp.py`
Pulled from `test_parity_round2.py`: `test_repo_index_search_finds_identifier_variants`,
`test_repo_search_tool_end_to_end`, `test_lsp_client_initialize_diagnostics_definition`,
`test_lsp_tools_graceful_without_servers`.
Plus `test_round3_upgrades.py`'s `test_repo_index_boosts_defining_file_over_mentioning_file`,
`test_repo_index_symbols_survive_incremental_rebuild`; `test_review_round_fixes.py`'s
`test_symbol_terms_precomputed_at_index_time`; `test_design_review_r1.py`'s
`test_lsp_clients_close_all`.

### `test_recall_memory.py`
Full files: `test_recall.py`, `test_project_memory.py`.
Plus `test_functionality_review_r1.py`'s `test_recall_indexes_real_session_shape`,
`test_recall_memory_search_tool_end_to_end`.

### `test_modes.py`
Full file: `test_assistant_mode.py`.
Plus `test_competitive_parity.py`'s `test_plan_mode_hides_mutation_tools_and_marks_prompt`,
`test_plan_mode_defaults_off_and_cfg_flag_applies`; `test_round2_bugfixes.py`'s
`test_plan_mode_exposes_new_observation_tools`.

### `test_usage_metrics.py`
Full file: `test_usage.py`.
Plus `test_competitive_parity.py`'s `test_budget_stop_aborts_run_with_budget_reason`,
`test_no_budget_by_default`, `test_cost_estimation_known_and_unknown_models`,
`test_usage_summary_includes_estimated_cost`; `test_round1_features.py`'s
`test_usage_summary_metrics_fields`, `test_api_metrics_endpoint`; `test_round5_ease.py`'s
`test_render_metrics_text_empty_and_full`, `test_repl_slash_metrics_dispatches`,
`test_webui_slash_metrics_notice`; `test_review_fixes_r4.py`'s
`test_metrics_endpoint_days_parameter_extends_window`; `test_round2_bugfixes.py`'s
`test_cli_run_records_usage`, `test_repl_turn_records_usage`; `test_usage.py`'s own
`test_chat_turn_records_usage_and_state_exposes` (already there).

### `test_eval.py`
Full files: `test_eval_agent.py` (minus the two subagent tests, see `test_subagents.py`
below), `test_verify_project.py`.
Plus `test_round1_features.py`'s `test_verify_command_wired_from_cfg`;
`test_design_review_r1.py`'s `test_verify_command_reaches_live_tools`,
`test_sandboxed_without_backend_keeps_friction_and_warns`.

### `test_gateway.py`
Pulled from `test_v05_platform.py`: `test_telegram_gateway_end_to_end`,
`test_gateway_session_reuse_and_error_path`.
Plus `test_round2_regressions.py`'s `test_gateway_backoff_on_transport_failure`.

### `test_screen_ocr_input.py`
Full file: `test_ocr_and_unix.py`.
Plus `test_v05_platform.py`'s `test_screen_tool_captures_real_screen`;
`test_shell_gui_hang.py` (both tests, `test_gui_spawn_command_returns_within_timeout`,
`test_winjob_assign_and_terminate`).

### `test_web_browser_search.py`
Pulled from `test_frontier_features.py`: `test_extract_readable_text_and_links`,
`test_web_search_parses_live_style_fixture`, `test_web_search_decodes_uddg_exactly_once`,
`test_web_search_zero_parse_is_error`, `test_browser_open_click_back`,
`test_browser_click_without_open`.
Plus `test_v05_platform.py`'s `test_playwright_adapter_degrades_without_dep`,
`test_playwright_registered_only_when_available`, `test_playwright_adapter_present_when_importable`;
full file `test_playwright_e2e.py`.

### `test_skills.py`
Pulled from `test_frontier_features.py`: `test_skill_store_roundtrip`,
`test_skill_store_reads_hermes_agentskills_format`, `test_skills_prompt_block_empty_store`,
`test_learning_plugin_registers_skill_tools`.
Plus the vision-image tests from the same file, which are really a separate subject
(vision attachments, not skills) — put `test_view_image_registry_image_transfer`,
`test_loop_attaches_tool_images_as_vision_message`,
`test_attachments_become_vision_parts_in_first_message` into `test_agent_loop.py` instead
(vision-in-messages is a loop/message concern), plus `test_review_fixes.py`'s
`test_review1_vision_attachments_via_facade` (same bucket).

### `test_subagents.py`
Pulled from `test_parity_round2.py`: `test_subagent_continuation_keeps_child_context`,
`test_background_subagent_reports_via_job_manager`, `test_legacy_runner_contract_still_works`.
Plus `test_review_fixes.py`'s `test_review8_subagents_do_not_recurse`,
`test_review2_plugin_path_shares_job_manager`; `test_eval_agent.py`'s
`test_subagent_tool_reports_and_errors`; `test_round2_bugfixes.py`'s
`test_job_list_survives_background_subagent_jobs`,
`test_reap_keeps_live_agent_jobs_and_old_process_jobs_semantics`; `test_review_fixes_r4.py`'s
`test_reap_never_drops_running_subagent_jobs`; `test_round2_regressions.py`'s
`test_default_registry_shares_job_manager`.

### `test_repl_cli.py`
Full file: `test_repl_app.py`.
Plus `test_audit2_fixes.py`'s `test_keyboard_long_text_chunked_under_command_limit`,
`test_repl_dispatch_errors_do_not_kill_session`, `test_repl_run_survives_failing_dispatch`,
`test_second_repl_does_not_double_chain_gate`; `test_round2_bugfixes.py`'s
`test_repl_model_persists_to_config`; `test_round5_ease.py`'s
`test_unknown_provider_did_you_mean`, `test_doctor_reports_invalid_local_json`,
`test_doctor_uses_provider_specific_probe`, `test_doctor_offline_skips_probe_and_never_fails_on_endpoint`,
`test_init_mentions_chat_and_doctor`; `test_providers.py`'s own
`test_cli_provider_choices_cover_majors`, `test_unknown_provider_lists_available` (these two
are borderline, fine to leave in `test_providers.py` too if that's less churn — pick one
during execution, don't duplicate); `test_round1_features.py`'s
`test_init_scaffolds_idempotently_with_force`, `test_init_registered_in_parser`;
`test_v06_features.py`'s `test_repl_context_command_renders_breakdown`.

### `test_hooks.py`
Pulled from `test_parity_round2.py`: `test_hooks_file_blocks_tool_call`,
`test_hook_exit_zero_allows_and_crash_does_not_block`,
`test_agent_run_chains_user_hooks_after_safety`.
Plus `test_review_fixes.py`'s `test_review7_custom_hook_chains_with_safety`;
`test_round2_regressions.py`'s `test_agent_hook_wrappers_do_not_compound`;
`test_alignment.py`'s `test_pre_tool_call_block` (fits here too, or leave in
`test_agent_loop.py`, both defensible — pick one, don't duplicate);
`test_design_review_r1.py`'s `test_install_web_surface_chains_pre_existing_hook`.

### `test_config_and_startup.py`
Pulled from `test_design_review_r1.py`: `test_config_fields_all_propagate_or_rebuild`,
`test_shell_allow_network_refuses_when_unenforceable`, `test_shell_allow_network_default_runs`,
`test_shell_allow_network_read_dynamically`, `test_eventbus_subscriber_queue_is_bounded`,
`test_appstate_evicts_idle_runtimes`, `test_editing_module_is_single_source_of_truth`,
`test_no_surface_to_surface_imports`.
Plus `test_round1_features.py`'s `test_state_and_apply_config_roundtrip_new_knobs`;
`test_v06_features.py`'s `test_version_bumped`, `test_title_from_text_strips_noise`;
`test_production_hardening.py`'s `test_config_dir_patch_propagates_to_file`,
`test_config_file_explicit_override_still_wins`, `test_classify_error_kinds`
(wait — `test_classify_error_kinds` and `test_classify_error_tuple_api_unchanged`
are really error-taxonomy tests, put both in `test_agent_loop.py` instead, they're about
how the loop classifies provider errors, not config/startup);
`test_production_hardening.py`'s remaining config-shaped tests:
`test_trajectory_messages_match_live_history`, `test_webui_reexports_intact` (small, could
also just live in `test_webui_core.py` — pick one).

### `test_provider_overflow.py`
A genuinely distinct small cluster worth its own file rather than forcing it into
`test_context_and_compaction.py` or `test_config_and_startup.py`:
`test_production_hardening.py`'s `test_provider_overflow_marker_triggers_context_overflow`,
`test_non_marked_bad_request_stays_bad_request`; `test_product_hardening.py`'s
`test_overflow_triggers_force_compaction_then_retry`, `test_shell_spills_large_output`,
`test_strip_think_reasoning_passback_flag`, `test_chat_falls_back_to_backup_model`,
`test_chat_honors_retry_after_header`, `test_context_overflow_raises_through_immediately`,
`test_sessions_roundtrip`, `test_compact_preserves_goal_verbatim` (last two are really
`test_checkpoints_sessions.py`/`test_context_and_compaction.py` respectively — move them
there instead, don't leave them here just because the source file put them next to
overflow tests).

### `test_cron.py`
Full file: `test_schedule.py`.
Plus `test_functionality_review_r1.py`'s `test_cron_dow_7_matches_sunday`,
`test_cron_dow_7_end_to_end`.

### `test_search_index.py`
Full file: `test_search_onboard.py` — note despite the name this file is about the
in-app search endpoint and the onboarding *write* flow (`test_onboard_writes_env_switches_provider`
etc.), not the same "onboarding" as `test_onboarding.py` (which is provider-probe logic).
Keep this file's content together as one unit under a clearer name, don't merge into
`test_onboarding.py`, they test different code paths despite the naming collision.

---

## Confirmed-by-name-only duplicate candidates (do NOT delete without a real diff)

These pairs look like the same behavior tested twice from different review passes.
Flagging only, a real diff of each pair's assertions is required before treating either
as removable:

- `test_review_fixes.py::test_review3_session_id_passthrough` vs
  `test_review_fixes.py::test_session_create_unique_under_collision` — same file, may
  already be intentionally distinct (collision vs passthrough), lower confidence.
- `test_providers.py::test_unknown_provider_lists_available` vs
  `test_round5_ease.py::test_unknown_provider_did_you_mean` — both about the unknown-provider
  error path, plausibly the same case with the second added later to cover a message
  wording refinement. Worth a real look.
- `test_desktop_window.py::test_embedded_window_falls_back_without_pywebview` vs anything
  the currently-excluded computer-use files might have on the same fallback — check once
  those five files are back in scope.

No other pairs looked like real duplicates from names alone; most apparent overlaps
(e.g. multiple `test_config_*` or `test_trust_*` named tests) turned out to test distinct
cases on the same subject, not the same case twice.

---

## Execution notes for whoever runs this

1. Do this as its own isolated pass, not mixed with any other change, so a revert is
   possible without losing unrelated work.
2. Move don't retype: use `git mv` where a file becomes a straight rename with additions,
   otherwise cut/paste function bodies verbatim, don't reformat or "clean up" while moving —
   that's a separate, later pass, and mixing the two makes the diff impossible to review.
3. After each new target file is assembled, run just that file
   (`pytest tests/test_X.py -q`) before moving to the next, catching import/fixture
   breakage early rather than at the end.
4. At the end, `pytest --collect-only -q` must report the same 715 total (minus any
   confirmed-real duplicate removed on purpose, noted in the commit message) — a lower
   number with no explanation is a bug in the move, not a cleanup win.
5. Check `conftest.py` and `fakes.py` for fixtures scoped or imported per-file — moving a
   test out of its original file can silently break a fixture that was only defined
   locally in that file rather than in `conftest.py`. Grep for `def test_` neighbors that
   share a local fixture before splitting them apart.
