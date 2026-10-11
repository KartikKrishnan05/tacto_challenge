"""Strategy knobs, the learned playbook and how the agent picks a strategy for a case."""

import json

import pytest

from arena_kit.spec import lab, playbook, prep
from arena_kit.spec import strategy as strat
from arena_kit.spec.concession import Plan
from tests.spec_support import CASES


def row(case_id, variant, score, mode="code", type_="raw_material", goal="leverage", n=1, **kw):
    return {"case_id": case_id, "variant": variant, "score": score, "mode": mode, "type": type_, "goal": goal,
            "rfq": n > 1, "n_suppliers": n, "batch": "b", "label": "t", **kw}  # fmt: skip


def case(case_id, type_="raw_material", goal="leverage", n=1):
    return {"case_id": case_id, "type": type_, "buyer": {"category_goal": goal},
            "suppliers": [f"s{i}" for i in range(n)]}  # fmt: skip


def test_base_strategy_leaves_the_playbook_unchanged():
    c = CASES["case:P01"]
    assert strat.BASE.row(c) is playbook.row(c)
    assert strat.BASE.horizon(c) == playbook.horizon(c)
    p = prep.prepare(c)
    assert p.row is playbook.row(c) and p.horizon == playbook.horizon(c)
    assert Plan(**strat.BASE.plan_kwargs()) == Plan()


def test_variant_changes_anchor_cut_close_gap_and_horizon():
    c = CASES["case:P18"]
    s = strat.Strategy(cut_k=2.0, close_gap_k=3.0, horizon_delta=-10, anchor_cost_k=0.9)
    r = s.row(c)
    assert r["cut"] == pytest.approx(2 * playbook.row(c)["cut"])
    assert r["close_gap"] == pytest.approx(3 * playbook.row(c)["close_gap"])
    assert r["anchor_cost_k"] == 0.9
    assert s.horizon(c) == strat.MIN_HORIZON
    assert prep.prepare(c, strategy=s).row["cut"] == pytest.approx(r["cut"])
    assert playbook.row(c)["cut"] != r["cut"]  # the shared table is not modified


def test_deeper_cut_lowers_or_keeps_the_anchor():
    c = CASES["case:P18"]
    base = prep.prepare(c)
    deep = prep.prepare(c, strategy=strat.VARIANTS["deep_anchor"])
    soft = prep.prepare(c, strategy=strat.VARIANTS["soft_anchor"])
    for sid, o in base.openings.items():
        assert deep.openings[sid].anchor <= o.anchor <= soft.openings[sid].anchor


def test_concession_knobs_change_the_step():
    slow = Plan(**strat.VARIANTS["slow_concede"].plan_kwargs())
    fast = Plan(**strat.VARIANTS["fast_concede"].plan_kwargs())
    for p in (slow, fast):
        p.set_anchor(900_000)
        p.observe(1_000_000, 1)
        p.commit(p.next_target(1_000_000, 6)[0])
        p.observe(990_000, 2)
    assert slow.next_target(990_000, 6)[0] < fast.next_target(990_000, 6)[0]


def test_no_playbook_plays_base():
    s, name, why = strat.choose(case("case:X"), None)
    assert s is strat.BASE and name == "base"


def test_build_and_choose_most_specific_match():
    rows = [
        row("case:A", "base", 0.40), row("case:A", "deep_anchor", 0.46), row("case:A", "soft_anchor", 0.38),
        row("case:B", "base", 0.50), row("case:B", "deep_anchor", 0.53), row("case:B", "soft_anchor", 0.50),
        row("case:C", "base", 0.60, type_="renewal", goal="stabilise"),
        row("case:C", "soft_anchor", 0.66, type_="renewal", goal="stabilise"),
        row("case:C", "deep_anchor", 0.58, type_="renewal", goal="stabilise"),
        row("case:D", "base", 0.9, error="boom"),  # failed games are left out
    ]  # fmt: skip
    kb = lab.build_playbook(rows)
    assert set(kb["cases"]) == {"case:A", "case:B", "case:C"}
    assert kb["cases"]["case:A"]["gains"]["deep_anchor"]["gain"] == pytest.approx(0.06)
    # the case itself
    assert strat.choose(case("case:A"), kb)[1] == "deep_anchor"
    # unseen: same type + goal -> case B alone says deep_anchor
    s, name, why = strat.choose(case("case:A"), kb, exclude_self=True)
    assert name == "deep_anchor" and "type raw_material + goal leverage" in why
    # a new case of another type with goal stabilise -> soft_anchor via the goal
    assert strat.choose(case("case:new", type_="capacity", goal="stabilise"), kb)[1] == "soft_anchor"
    # nothing comparable: a variant that won 2+ cases and lost none (here none: deep_anchor lost C, soft_anchor A)
    s, name, why = strat.choose(case("case:new", type_="quality", goal="quality_first"), kb)
    assert name == "base" and why.startswith("all cases")
    # renewal / stabilise: case C's soft_anchor (+0.06; its loss on A is only 0.02)
    assert strat.choose(case("case:C2", type_="renewal", goal="stabilise"), kb, exclude_self=True)[1] == "soft_anchor"


def test_a_variant_that_loses_a_case_badly_is_not_chosen_for_the_group():
    rows = [row("case:A", "base", 0.4), row("case:A", "deep_anchor", 0.6),
            row("case:B", "base", 0.5), row("case:B", "deep_anchor", 0.4)]  # fmt: skip
    kb = lab.build_playbook(rows)
    assert strat.choose(case("case:new"), kb)[1] == "base"  # +0.05 mean, but -0.10 on B


def test_gains_are_measured_within_one_mode():
    rows = [row("case:A", "base", 0.4, mode="code"), row("case:A", "eager_close", 0.5, mode="model")]
    kb = lab.build_playbook(rows)
    assert kb["cases"]["case:A"]["gains"] == {}  # no model-mode base to compare with


def test_build_writes_the_sheet_and_report(tmp_path):
    rows = [row("case:A", "base", 0.4, knobs=strat.BASE.knobs()),
            row("case:A", "fast_concede", 0.45, knobs=strat.VARIANTS["fast_concede"].knobs())]  # fmt: skip
    (tmp_path / lab.SHEET).write_text("".join(json.dumps(r) + "\n" for r in rows))
    kb = lab.build(tmp_path)
    assert json.loads((tmp_path / strat.PLAYBOOK_FILE).read_text())["cases"]["case:A"] == kb["cases"]["case:A"]
    md = (tmp_path / "EXPERIMENTS.md").read_text()
    assert "fast_concede" in md and "case:A" in md
    assert (tmp_path / "experiments.csv").read_text().count("\n") == 3
    assert strat.choose(case("case:A"), strat.load(tmp_path / strat.PLAYBOOK_FILE))[1] == "fast_concede"


def test_agent_uses_fixed_strategy_then_learned_playbook():
    from arena_kit.agents.spec import SpecAgent

    c = CASES["case:P18"]
    a = SpecAgent(strategy=strat.VARIANTS["slow_concede"])
    a.choose_strategy(c)
    assert a.strategy_name == "slow_concede"
    kb = lab.build_playbook([row(c["case_id"], "base", 0.3), row(c["case_id"], "eager_close", 0.4)])
    b = SpecAgent(knowledge=kb)
    b.choose_strategy(c)
    assert b.strategy_name == "eager_close" and b.strategy is strat.VARIANTS["eager_close"]
    assert SpecAgent().strategy is strat.BASE


def test_group_picks_must_be_safe_everywhere_and_ties_go_to_the_simpler_variant():
    rows = [
        row("case:A", "base", 0.40), row("case:A", "firm_close", 0.50), row("case:A", "firm_deep_fast", 0.50),
        row("case:B", "base", 0.30), row("case:B", "firm_deep_fast", 0.55), row("case:B", "fast_concede", 0.40),
        row("case:B", "firm_close", 0.30),
        row("case:R", "base", 0.75, type_="rfq", n=3), row("case:R", "firm_deep_fast", 0.57, type_="rfq", n=3),
        row("case:R", "fast_concede", 0.75, type_="rfq", n=3), row("case:R", "firm_close", 0.75, type_="rfq", n=3),
    ]  # fmt: skip
    kb = lab.build_playbook(rows)
    assert strat.choose(case("case:A"), kb)[1] == "firm_close"  # tie at +0.10: one knob beats seven
    assert strat.choose(case("case:B"), kb)[1] == "firm_deep_fast"  # its own evidence
    # a new raw_material/leverage case: firm_deep_fast leads the group, but it broke case R by 0.18
    assert strat.choose(case("case:new"), kb)[1] == "fast_concede"


def test_all_cases_level_needs_two_wins_and_no_loss():
    rows = [row("case:A", "base", 0.4), row("case:A", "firm_close", 0.5), row("case:A", "deep_fast", 0.6),
            row("case:B", "base", 0.4), row("case:B", "firm_close", 0.45), row("case:B", "deep_fast", 0.39)]
    kb = lab.build_playbook(rows)
    s, name, why = strat.choose(case("case:new", type_="quality", goal="quality_first"), kb)
    assert name == "firm_close" and why.startswith("all cases")  # deep_fast has the higher mean but lost B


def test_briefing_names_what_helped_and_what_hurt_for_the_goal():
    rows = [row("case:A", "base", 0.4), row("case:A", "firm_close", 0.5), row("case:A", "soft_anchor", 0.2),
            row("case:B", "base", 0.4, type_="renewal", goal="routine")]  # fmt: skip
    kb = lab.build_playbook(rows)
    lines = strat.briefing(case("case:new"), kb)
    assert any("firm_close gained" in x for x in lines) and any("soft_anchor lost" in x for x in lines)
    assert strat.briefing(case("case:new"), None) == []


def test_a_group_with_no_winner_passes_on_to_a_variant_safe_everywhere():
    rows = [row("case:A", "base", 0.4, type_="t1", goal="g1"),
            row("case:A", "firm_one_step", 0.4, type_="t1", goal="g1"),
            row("case:B", "base", 0.3), row("case:B", "firm_one_step", 0.36),
            row("case:C", "base", 0.4), row("case:C", "firm_one_step", 0.45)]  # fmt: skip
    kb = lab.build_playbook(rows)
    s, name, why = strat.choose(case("case:new", type_="t1", goal="g1"), kb)
    assert name == "firm_one_step" and why.startswith("all cases")
