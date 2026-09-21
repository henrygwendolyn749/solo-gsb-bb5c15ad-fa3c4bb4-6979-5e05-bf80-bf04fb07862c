"""FastAPI application: task creation, revision and result query."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import asynccontextmanager
from pydantic import ValidationError

from . import database
from .schemas import (
    ExamTaskIn,
    ReviseTaskIn,
    TaskCreated,
    TaskResult,
    TaskStatus,
    TemplateConstraints,
    TemplateCreateIn,
    TemplateOut,
    TemplateTaskIn,
)
from .worker import submit


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="智能组卷 API",
    description=(
        "面向教研组的题库快照约束组卷服务（FastAPI + SQLite + OR-Tools CP-SAT），"
        "支持 2~5 份平行卷联合组卷、1~5 个差异化候选卷组，"
        "以及可版本化的题库无关组卷模板（实例化时按白名单覆盖约束）"
    ),
    version="1.4.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 组卷模板：可版本化、创建后不可原地修改
# --------------------------------------------------------------------------- #
#: 实例化时允许被 overrides 覆盖的约束字段（即模板约束全集；questions/seed/name
#: 等实例化参数不在其列）
OVERRIDABLE_FIELDS = frozenset(TemplateConstraints.model_fields)


def _template_out(row) -> TemplateOut:
    return TemplateOut(
        template_id=row["template_id"],
        version=row["version"],
        latest_version=database.get_latest_version(row["template_id"]),
        name=row["name"],
        created_at=row["created_at"],
        config=TemplateConstraints(**json.loads(row["config"])),
    )


@app.post("/api/templates", response_model=TemplateOut, status_code=201)
def create_template(payload: TemplateCreateIn) -> TemplateOut:
    """创建组卷模板：题库无关的约束配置，创建后不可修改。

    缺省创建全新模板（version=1）；传入已有 template_id 则在其下生成递增的
    新版本，历史版本保持可查询、可实例化。
    """
    config = payload.model_dump(exclude={"name", "template_id"}, exclude_none=True)
    try:
        template_id, version = database.create_template(
            payload.name,
            json.dumps(config, ensure_ascii=False),
            payload.template_id,
        )
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail=f"模板 {payload.template_id} 不存在，无法在其下创建新版本",
        )
    return _template_out(database.get_template(template_id, version))


@app.get("/api/templates/{template_id}", response_model=TemplateOut)
def get_template(
    template_id: int, version: Optional[int] = Query(None, ge=1)
) -> TemplateOut:
    """查询模板：缺省返回最新版本，?version=N 返回指定历史版本。"""
    row = database.get_template(template_id, version)
    if row is None:
        if version is not None and database.get_latest_version(template_id) is not None:
            raise HTTPException(
                status_code=404, detail=f"模板 {template_id} 不存在版本 {version}"
            )
        raise HTTPException(status_code=404, detail="模板不存在")
    return _template_out(row)


@app.post("/api/templates/{template_id}/tasks", response_model=TaskCreated, status_code=201)
def create_task_from_template(template_id: int, payload: TemplateTaskIn) -> TaskCreated:
    """用模板实例化组卷任务（异步）：新的题库快照 + seed + 可选白名单 overrides。

    overrides 仅允许覆盖模板约束字段，未知字段、null 值或合并后非法（如
    单卷下设置 max_shared_questions、互斥组引用不存在的题号）均返回 422。
    实际生效配置与 template_id/version 一并冻结进任务记录。
    """
    row = database.get_template(template_id, payload.version)
    if row is None:
        if payload.version is not None and database.get_latest_version(template_id) is not None:
            raise HTTPException(
                status_code=404,
                detail=f"模板 {template_id} 不存在版本 {payload.version}",
            )
        raise HTTPException(status_code=404, detail="模板不存在")
    config = json.loads(row["config"])

    overrides = payload.overrides or {}
    unknown = sorted(set(overrides) - OVERRIDABLE_FIELDS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"overrides 包含未知或不允许覆盖的字段: {unknown}；"
                f"可覆盖字段: {sorted(OVERRIDABLE_FIELDS)}"
            ),
        )
    null_keys = sorted(k for k, v in overrides.items() if v is None)
    if null_keys:
        raise HTTPException(
            status_code=422, detail=f"overrides 字段不允许取 null: {null_keys}"
        )

    # 合并模板约束与白名单 overrides，连同题库快照走与 /api/tasks 相同的完整校验
    effective: Dict[str, Any] = {**config, **overrides}
    effective["name"] = payload.name if payload.name is not None else row["name"]
    effective["questions"] = [q.model_dump() for q in payload.questions]
    effective["seed"] = payload.seed
    try:
        exam = ExamTaskIn(**effective)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(loc) for loc in e['loc']) or '(body)'}: "
            f"{e['msg'].removeprefix('Value error, ')}"
            for e in exc.errors()
        )
        raise HTTPException(
            status_code=422, detail=f"模板与 overrides 合并后的配置非法: {detail}"
        )

    # 冻结实际生效配置与模板溯源信息；求解路径与普通任务完全一致
    req = exam.model_dump()
    req["kind"] = "template"
    req["template_id"] = row["template_id"]
    req["template_version"] = row["version"]
    task_id = database.create_task(
        req["name"], json.dumps(req, ensure_ascii=False), payload.seed
    )
    submit(task_id)
    return TaskCreated(task_id=task_id, status="pending")


@app.post("/api/tasks", response_model=TaskCreated, status_code=201)
def create_task(payload: ExamTaskIn) -> TaskCreated:
    """提交组卷任务：题库快照 + 总分/题型/知识点/难度/互斥题组约束。"""
    req = payload.model_dump()
    task_id = database.create_task(
        payload.name,
        json.dumps(req, ensure_ascii=False),
        payload.seed,
    )
    submit(task_id)
    return TaskCreated(task_id=task_id, status="pending")


def _source_paper_selection(
    source_req: Dict[str, Any], source_result: Dict[str, Any]
) -> List[List[Any]]:
    """从源任务结果中提取逐卷选中题号（单卷 -> 单元素列表）。

    多候选源任务以排名第 1（择优最优）的候选为改卷基准。
    """
    paper_count = int(source_req.get("paper_count") or 1)
    alts = source_result.get("alternatives")
    if alts:
        top = min(alts, key=lambda a: a.get("rank", 1))
        if paper_count == 1:
            return [list(top.get("selected_ids") or [])]
        return [list(p.get("selected_ids") or []) for p in top.get("papers") or []]
    if paper_count == 1:
        return [list(source_result.get("selected_ids") or [])]
    return [
        list(p.get("selected_ids") or [])
        for p in source_result.get("papers") or []
    ]


@app.post("/api/tasks/{task_id}/revise", response_model=TaskCreated, status_code=201)
def revise_task(task_id: int, payload: ReviseTaskIn) -> TaskCreated:
    """基于已完成且可行的源任务创建定向改卷任务（异步）：按卷指定必须保留/移除的题号。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "done":
        raise HTTPException(status_code=422, detail="源任务尚未完成，无法改卷")

    source_req = json.loads(row["request"])
    # 改卷以最近一次组卷/改卷的原始请求与源卷选择为准（支持对改卷结果再次改卷）
    if source_req.get("kind") == "revise":
        source_req = source_req["source_request"]
    if not row["result"]:
        raise HTTPException(status_code=422, detail="源任务尚无结果，无法改卷")
    source_result = json.loads(row["result"])
    if source_result.get("status") != "feasible":
        raise HTTPException(status_code=422, detail="源任务结果不可行，无法改卷")

    paper_count = int(source_req.get("paper_count") or 1)
    id_set = {q["id"] for q in source_req["questions"]}

    # 卷序号不可重复；不在 [0, paper_count) 内或题号不存在均返回 422
    seen_papers = set()
    for e in payload.edits:
        if e.paper in seen_papers:
            raise HTTPException(status_code=422, detail=f"第 {e.paper} 卷的编辑条件重复指定")
        seen_papers.add(e.paper)
        if e.paper >= paper_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"卷序号 {e.paper} 不存在：源任务共 {paper_count} 份卷"
                    f"（卷序号 0~{paper_count - 1}）"
                ),
            )
        missing = [qid for qid in (e.retain_ids + e.remove_ids) if qid not in id_set]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"第 {e.paper} 卷引用了题库中不存在的题号: {missing}",
            )

    # 冻结源请求与逐卷人工编辑条件；源任务本身保持不变
    frozen = {
        "kind": "revise",
        "source_task_id": task_id,
        "source_request": source_req,
        "source_selection": _source_paper_selection(source_req, source_result),
        "edits": [e.model_dump() for e in payload.edits],
    }
    seed = int(source_req.get("seed") or 42)
    name = payload.name if payload.name is not None else row["name"]
    new_task_id = database.create_task(
        name,
        json.dumps(frozen, ensure_ascii=False),
        seed,
    )
    submit(new_task_id)
    return TaskCreated(task_id=new_task_id, status="pending")


@app.get(
    "/api/tasks/{task_id}",
    response_model=TaskStatus,
    response_model_exclude_none=True,
)
def get_task(task_id: int) -> TaskStatus:
    """查询任务状态与组卷结果（可行卷或最小冲突约束集）。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    result: Optional[TaskResult] = None
    if row["result"]:
        result = TaskResult(**json.loads(row["result"]))

    source_task_id: Optional[int] = None
    template_id: Optional[int] = None
    template_version: Optional[int] = None
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        source_task_id = req.get("source_task_id")
        # 改卷任务的模板溯源信息冻结在源请求中
        source_req = req.get("source_request") or {}
        template_id = source_req.get("template_id")
        template_version = source_req.get("template_version")
    else:
        template_id = req.get("template_id")
        template_version = req.get("template_version")

    return TaskStatus(
        task_id=row["id"],
        status=row["status"],
        name=row["name"],
        seed=row["seed"],
        created_at=row["created_at"],
        source_task_id=source_task_id,
        template_id=template_id,
        template_version=template_version,
        result=result,
    )
