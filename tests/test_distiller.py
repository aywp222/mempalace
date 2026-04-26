from mempalace.distiller import _tier, _text_score, report_markdown


def test_distiller_scores_bugfix_memory_as_high_signal():
    text = "[2026-04-26] 搜索不可用\n根因：MCP 客户端没有暴露 search 工具\n解决方案：统一 HTTP 服务并刷新配置\n相关文件：proxy.py"
    score, reasons = _text_score(text, {"room": "bugfix", "source_file": "notes.md"})

    assert score >= 7
    assert "important_room:bugfix" in reasons
    assert any(reason.startswith("signal_terms:") for reason in reasons)


def test_distiller_marks_generated_code_as_lower_signal():
    text = "function x(){return a.b[c](d);};" * 80 + "\n//# sourceMappingURL=bundle.min.js.map"
    score, reasons = _text_score(text, {"room": "general", "source_file": "app.min.js"})

    assert score < 2
    assert "review_room:general" in reasons
    assert any(reason.startswith("low_signal_terms:") for reason in reasons)


def test_closet_index_penalty_keeps_index_entries_out_of_promotion():
    text = "fixed build error|Unity;Fix|→drawer_abc drawer_def"
    score, reasons = _text_score(text, {"room": "gouji", "source_file": "Packages/foo/CHANGELOG.md"})
    if "closet_abc".startswith("closet_"):
        score -= 8
        reasons.append("closet_index")

    assert _tier(score, min_score=7) != "promote"
    assert "closet_index" in reasons


def test_report_markdown_contains_policy_and_candidates():
    report = {
        "generated_at": "2026-04-26T00:00:00",
        "scanned": 1,
        "rooms": [
            {
                "wing": "mshl",
                "room": "bugfix",
                "total": 1,
                "promote": 1,
                "review": 0,
                "archive_candidate": 0,
                "top": [
                    {
                        "score": 9,
                        "tier": "promote",
                        "drawer_id": "drawer_1",
                        "source_file": "notes.md",
                        "reasons": ["important_room:bugfix"],
                        "preview": "根因：配置不一致",
                    }
                ],
            }
        ],
    }

    text = report_markdown(report)

    assert "MemPalace Distillation Report" in text
    assert "mshl / bugfix" in text
    assert "drawer_1" in text