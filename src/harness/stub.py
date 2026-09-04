from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .protocol import AgentExecutionResult, AgentInvocation


class StubHarness:
    """Deterministic harness for tests; never exposed as a production CLI choice."""

    def __init__(
        self,
        responder: Optional[Callable[[AgentInvocation], str]] = None,
        fail_node: Optional[str] = None,
    ) -> None:
        self._responder = responder or self._default_response
        self._fail_node = fail_node
        self.started = False
        self.invocations: list[AgentInvocation] = []
        self.cancelled: list[str] = []

    async def preflight(self) -> Mapping[str, Any]:
        return {"harness": "stub", "ok": True}

    async def start(self) -> None:
        self.started = True

    async def invoke(self, invocation: AgentInvocation) -> AgentExecutionResult:
        if not self.started:
            raise RuntimeError("stub harness has not been started")
        self.invocations.append(invocation)
        if self._fail_node == invocation.node_type:
            return AgentExecutionResult(
                status="failed",
                native_session_id=f"stub-{invocation.invocation_id}",
                error=f"injected failure for {invocation.node_type}",
            )
        self._materialize_stub_outputs(invocation)
        return AgentExecutionResult(
            status="succeeded",
            response_text=self._responder(invocation),
            native_session_id=f"stub-{invocation.invocation_id}",
            stop_reason="end_turn",
            events=[{"sessionUpdate": "agent_message_chunk", "stub": True}],
        )

    async def cancel(self, invocation_id: str) -> None:
        self.cancelled.append(invocation_id)

    async def close(self) -> None:
        self.started = False

    @staticmethod
    def _default_response(invocation: AgentInvocation) -> str:
        return f"# {invocation.node_type}\n\nStub output for {invocation.node_type}."

    @classmethod
    def _materialize_stub_outputs(cls, invocation: AgentInvocation) -> None:
        """Write deterministic files through the public Node Context."""

        context = invocation.agent_context
        node_type = invocation.node_type
        outputs = context["outputs"]
        inputs = context["inputs"]
        scope = context["scope"]

        def output_path(port):
            return outputs[port]["path"]

        def input_paths(port):
            return [item["path"] for item in inputs.get(port, [])]

        if node_type == "scout":
            cls._write_json(output_path("briefing"), cls._stub_briefing())
        elif node_type == "plan":
            mode = context["run"]["mode"]
            plan = cls._stub_plan(mode)
            cls._write_json(output_path("plan"), plan)
        elif node_type == "research":
            dimension_id = scope.get("dimension-id", "d1")
            existing = input_paths("evidence")
            supplement_plans = input_paths("supplement-plan")
            if existing:
                evidence = json.loads(Path(existing[-1]).read_text(encoding="utf-8"))
            else:
                mode = "quick" if context["run"]["mode"] == "quick" else "initial"
                evidence = cls._stub_evidence(dimension_id, mode)
            cls._write_json(output_path("evidence"), evidence)
            if supplement_plans:
                completed_plan = json.loads(
                    Path(supplement_plans[-1]).read_text(encoding="utf-8")
                )
                for item in completed_plan.get("supplement_items", []):
                    item["status"] = "resolved"
                    item["resolution_note"] = "Resolved by the deterministic supplement pass."
                cls._write_json(
                    output_path("completed-supplement-plan"), completed_plan
                )
        elif node_type == "review":
            dimension_id = scope.get("dimension-id", "d1")
            cls._write_text(
                output_path("review"),
                f"# Review {dimension_id}\n\n## 审查结论\n\nVERDICT: pass\n\n"
                "## 问题清单\n\n无。\n\n## 核验记录\n\n已核验正式输入。\n\n"
                "## 审查说明\n\nStub evidence passed.\n",
            )
        elif node_type == "perspective":
            dimension_id = scope.get("dimension-id", "d1")
            plan_paths = input_paths("plan")
            lenses = []
            if plan_paths:
                plan = json.loads(Path(plan_paths[-1]).read_text(encoding="utf-8"))
                dimension = next(item for item in plan["dimensions"] if item["id"] == dimension_id)
                lenses = dimension.get("lenses", [])
            lens_body = ""
            if lenses:
                for index, lens in enumerate(lenses, start=1):
                    lens_body += (
                        f"### l{index}: {lens['axis']}:{lens['value']}\n\n"
                        "#### Lens 定位\n\nStub lens.\n\n"
                        "#### 写作补充边界（非正文主张）\n\n无。\n\n"
                        "#### 需要补研后才能使用\n\n无。\n\n"
                        "#### 探索性搜索线索\n\n无。\n\n"
                    )
            else:
                lens_body = "当前维度没有已声明 lens。\n\n"
            cls._write_text(
                output_path("perspective"),
                f"# Perspective Summary: {dimension_id}\n\n## Lens Reviews\n\n{lens_body}"
                "## 维度内补研需求\n\n无必要补研。\n\n"
                "## 写回摘要\n\n- 无必要补研。\n- 无写作边界。\n- 无探索线索。\n",
            )
        elif node_type == "supplement-planner":
            dimension_id = scope.get("dimension-id", "d1")
            cls._write_json(
                output_path("supplement-plan"),
                {
                    "dimension_id": dimension_id,
                    "supplement_items": [],
                    "deferred_items": [],
                },
            )
        elif node_type == "report-planner":
            evidence_paths = input_paths("evidence")
            dimension_id = cls._first_evidence_dimension(evidence_paths)
            claim_id = f"{dimension_id}.c1"
            report_format = context.get("run", {}).get("report_format", "formal_report")
            cls._write_json(
                output_path("outline"), cls._stub_outline(claim_id, report_format)
            )
        elif node_type == "report-writer":
            task_paths = input_paths("task")
            if task_paths:
                source_id = cls._first_subset_source_id(task_paths[0])
                body = f"## Stub check\n\n- [x] Stub fact is supported.[^{source_id}]\n"
            else:
                source_id = json.loads(Path(input_paths("evidence")[0]).read_text())["sources"][0]["id"]
                body = f"# Stub Report\n\nStub output for ReportWriter.[^{source_id}]\n"
            cls._write_text(output_path("draft"), body)
        elif node_type == "stitcher":
            bodies = [Path(path).read_text(encoding="utf-8").strip() for path in input_paths("drafts")]
            cls._write_text(output_path("stitched"), "# Stub Report\n\n" + "\n\n".join(bodies) + "\n")
        elif node_type in {"final-review", "final-review-diagnostic", "final-review-recheck"}:
            cls._write_text(
                output_path("review"),
                "## 审查结论\n\nVERDICT: pass\n\n## 问题清单\n\n无。\n\n"
                "## 核验记录\n\n已核验正式输入。\n\n## 审查说明\n\nStub report passed.\n",
            )
        elif node_type == "final-repair":
            original = Path(input_paths("draft")[0]).read_text(encoding="utf-8")
            cls._write_text(output_path("draft"), original)
        elif node_type == "md-html":
            cls._write_text(output_path("report"), "<!doctype html><html><body><h1>Stub Report</h1></body></html>\n")

    @staticmethod
    def _write_json(path: str | Path, value: Mapping[str, Any]) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_text(path: str | Path, value: str) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value, encoding="utf-8")

    @staticmethod
    def _stub_plan(mode: str) -> Mapping[str, Any]:
        return {
            "dimensions": [
                {
                    "id": "d1",
                    "name": "Stub dimension",
                    "description": "Collect one deterministic fact",
                    "key_questions": ["What is the stub fact?"],
                    "focus": "A contract-valid evidence chain",
                    "sources": [
                        {"category": "official", "description": "Stub primary source"}
                    ],
                    "lenses": (
                        [
                            {
                                "axis": "stance",
                                "value": "skeptic",
                                "rationale": "Check the counter position",
                            }
                        ]
                        if mode == "heavy"
                        else []
                    ),
                    "depth": "skim",
                    "time_sensitivity": "Stable test fixture",
                    "scope_ownership": {
                        "owns": ["stub fact"],
                        "excludes": [],
                        "shared_topics": [],
                        "overlap_policy": "Only d1 owns the stub fact",
                    },
                }
            ],
        }

    @staticmethod
    def _stub_evidence(dimension_id: str, mode: str) -> Mapping[str, Any]:
        claim_id = f"{dimension_id}.c1"
        source_id = f"{dimension_id}_s1"
        return {
            "dimension_id": dimension_id,
            "headline": "A deterministic stub fact is available",
            "key_findings": [
                {"finding": "The stub fact is supported", "claim_ids": [claim_id]}
            ],
            "claims": [
                {
                    "id": claim_id,
                    "text": "The deterministic stub source supports the stub fact.",
                    "kind": "factual",
                    "polarity": "neutral",
                    "topic_tag": "stub_fact",
                    "evidence": [
                        {
                            "source_id": source_id,
                            "snippet": "The stub fact is supported.",
                            "quote_type": "direct",
                        }
                    ],
                }
            ],
            "sources": [
                {
                    "id": source_id,
                    "url": "https://example.com/stub",
                    "title": "Stub source",
                    "quality": "primary",
                    "published_at": "2026-01-01",
                }
            ],
            "writing_context": [],
        }

    @staticmethod
    def _stub_outline(
        claim_id: str, report_format: str = "formal_report"
    ) -> Mapping[str, Any]:
        return {
            "report_profile": {
                "format": report_format,
                "template_id": "general_research" if report_format == "formal_report" else None,
            },
            "paradigm": {"main": "evaluation", "secondary": None},
            "global_arc": "Present the deterministic fact and its evidence boundary.",
            "organization_decision": {
                "reader_task": "Check the deterministic fact",
                "opening_summary": "none",
                "toc": False,
                "numbered_headings": False,
            },
            "L0_draft": None,
            "style_contract": {
                "register": "research_brief",
                "voice": "neutral_analytical",
                "terminology": {"preferred": {}},
            },
            "content_units": [
                {
                    "id": "u1",
                    "type": "checklist",
                    "role": "primary",
                    "title": "Stub check",
                    "reader_task": "Check whether the fact is supported",
                    "lead": None,
                    "render_contract": {
                        "mode": "checklist",
                        "show_heading": True,
                        "schema": ["condition", "status", "evidence"],
                        "instructions": "Give one supported status with its source.",
                        "citation_policy": {
                            "scope": "element",
                            "require_each_claim": True,
                            "required_fields": [],
                        },
                        "secondary_structure": {
                            "allowed": False,
                            "required": False,
                            "heading_level": None,
                        },
                    },
                    "elements": [
                        {
                            "id": "e1",
                            "label": "Stub fact",
                            "purpose": "Show the supported stub fact",
                            "evidence_refs": [
                                {"claim_id": claim_id, "role": "primary_support"}
                            ],
                            "writing_context_refs": [],
                        }
                    ],
                }
            ],
            "scan_summary": {
                "conflicts": [],
                "gaps": [],
            },
        }

    @staticmethod
    def _first_evidence_dimension(evidence_paths: list[str]) -> str:
        value = json.loads(Path(evidence_paths[0]).read_text(encoding="utf-8"))
        return str(value["dimension_id"])

    @staticmethod
    def _first_subset_source_id(subset_path: str) -> str:
        value = json.loads(Path(subset_path).read_text(encoding="utf-8"))
        sources = value.get("sources", [])
        if sources and isinstance(sources[0], dict) and sources[0].get("id"):
            return str(sources[0]["id"])
        return "d1_s1"

    @staticmethod
    def _stub_briefing() -> Mapping[str, Any]:
        risk_names = [
            "时效性", "来源偏见", "口径不一致", "数据过时", "地区差异",
            "法规不确定", "营销话术", "缺一手证据", "幸存者偏差", "benchmark不可比",
        ]
        return {
            "user_confirmations_needed": {
                "blocking": [], "high_value": [], "optional": []
            },
            "task_interpretation": {
                "user_goal": "Validate the workflow",
                "requested_output_inferred": "A test report",
                "research_type_inferred": "tech_evaluation",
                "audience_inferred": "Test reader",
                "time_focus": "current",
                "explicit_constraints": [],
                "implicit_scope_hints": [],
            },
            "context_entities": [
                {
                    "name": f"Entity {index}",
                    "type": "concept",
                    "explicit_or_inferred": "inferred",
                    "why_it_matters": "Covers a distinct test entity",
                    "confidence": "medium",
                }
                for index in range(1, 6)
            ],
            "terminology": [],
            "subdomain_partitions": {
                "partition_basis": "by_topic",
                "subdomains": [
                    {"name": f"Topic {index}", "scope_hint": "Distinct scope"}
                    for index in range(1, 4)
                ],
            },
            "knowledge_topology": {
                "consensus": [
                    {"fact": "Consensus one", "source_hint": "Official source"},
                    {"fact": "Consensus two", "source_hint": "Academic source"},
                ],
                "disputes": [
                    {
                        "issue": "No material dispute found in the stub scan",
                        "positions_exist": [],
                        "representative_sources": [],
                    }
                ],
                "blanks": [],
            },
            "information_landscape": {
                "primary_source_categories": ["official"],
                "secondary_source_categories": ["news"],
                "data_source_categories": ["academic"],
                "expert_or_industry_sources": [],
                "weak_or_risky_sources": [],
                "high_value_urls": [
                    {"url": "https://example.com/official", "category": "official", "why": "Primary"},
                    {"url": "https://example.com/news", "category": "news", "why": "Secondary"},
                    {"url": "https://example.com/paper", "category": "academic", "why": "Research"},
                ],
                "search_terms": [],
                "time_sensitivity": {
                    "rate": "slow",
                    "recommended_window": "Current",
                    "reason": "Stable fixture",
                },
                "access_barriers": [],
            },
            "critical_unknowns": [],
            "candidate_lenses": [
                {
                    "lens": f"Lens {index}",
                    "useful_for": "Distinct coverage",
                    "may_miss": "Other coverage",
                }
                for index in range(1, 4)
            ],
            "coverage_boundary": {
                "adjacent_fields_not_explored": [],
                "opposing_perspectives_not_searched": [],
                "second_order_effects_not_explored": [],
                "alternative_paths_not_explored": [],
                "scan_scope": {
                    "zoom_level": "domain",
                    "scanned_angles": ["contract path"],
                    "unscanned_angles": ["production evidence"],
                },
                "lists_known_partial": {
                    "entities": {"more_likely_in": []},
                    "subdomains": {"alternative_partitions_exist": []},
                    "terminology": {"jargon_pockets_not_covered": []},
                    "unknowns": {"research_will_surface_more": True},
                    "disputes": {"more_likely_in": []},
                    "risks": {"more_likely_in": []},
                },
            },
            "hypotheses_to_test": [],
            "risk_flags": [
                {
                    "risk": risk,
                    "why_it_matters": "Required contract risk scan",
                    "mitigation": "Record the boundary",
                    "severity": "low",
                }
                for risk in risk_names
            ],
        }
