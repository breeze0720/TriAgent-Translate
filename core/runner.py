"""
core/runner.py - 图执行与落库的边界层

为什么要有这一层（而不是让节点直接写库）：
1. 节点是「可重放」的：checkpointer 恢复、条件边求值、测试重跑都可能让同一节点执行多次；
   节点里写库 = 隐藏的重复写（旧版 should_continue 写库就是要修的 bug 之一）。
2. 把副作用收口到执行边界后，节点只返回 state 更新，纯函数化，测试无需数据库。
3. 落库策略是「每个 superstep 后整份快照 UPDATE（按 task_id 幂等）+ 事件按
   (task_id, stage, version) upsert」，所以重放多少次结果都一样。

恢复逻辑（进程被 kill 后的验收点）：
lifespan 启动时扫描 status in ('running','pending')：
- running：有 checkpoint 快照 → graph.stream(None) 从断点续跑；没有快照 → 标记失败（可 /retry）；
- pending：从头开始（入参都在 DB 里，重新跑一遍即可，天然幂等）。
waiting_human 的任务不自动恢复——它在等人类，不能替用户点通过。
"""
import json
import threading
import traceback
from concurrent.futures import Executor
from datetime import datetime

from langgraph.types import Command

import config
import database
from core.events import publish
from workflow import STAGE_PROGRESS, build_initial_state, get_workflow

# 进程内正在执行的任务；用于阻止「同一个任务被 /retry 和恢复扫描同时跑」。
_active_tasks: set[int] = set()
_active_lock = threading.Lock()


def is_active(task_id: int) -> bool:
    with _active_lock:
        return task_id in _active_tasks


def _acquire(task_id: int) -> bool:
    with _active_lock:
        if task_id in _active_tasks:
            return False
        _active_tasks.add(task_id)
        return True


def _release(task_id: int) -> None:
    with _active_lock:
        _active_tasks.discard(task_id)


# ============================================
# 落库 / 事件
# ============================================

def _last_note(values: dict) -> str:
    history = values.get("history") or []
    if history and isinstance(history[-1], dict):
        return str(history[-1].get("note") or "")
    return ""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _progress_for(stage: str, status: str) -> int:
    if status in ("done", "failed"):
        return 100
    if status == "waiting_human":
        return STAGE_PROGRESS.get("human_review", 90)
    return STAGE_PROGRESS.get(stage, 0)


def _persist(task_id: int, values: dict, *, status: str | None = None, message: str = "") -> dict:
    """
    把当前 state 快照幂等落库，并广播 SSE 事件。返回事件 payload（便于测试断言）。

    幂等性：
    - tasks 以 task_id 为主键，整份快照覆盖写，重复执行同一 superstep 结果一致；
    - task_events 以 (task_id, stage, version) 唯一，重放不会插出重复进度。
    """
    stage = str(values.get("stage") or "init")
    effective_status = status or str(values.get("status") or "running")
    progress = _progress_for(stage, effective_status)
    review = values.get("review") or {}
    overall = review.get("overall")
    translation = values.get("translation") or ""
    history = values.get("history") or []
    version = int(values.get("version") or 0)

    snapshot = {
        "status": effective_status,
        "stage": stage,
        "progress": progress,
        "retry_count": int(values.get("retry_count") or 0),
        "score": float(overall) if overall is not None else None,
        "translation": translation,
        "polished": values.get("polished") or "",
        "final_result": translation if effective_status == "done" else "",
        "review_json": _json(review),
        "glossary_json": _json(values.get("glossary") or []),
        "segments_json": _json(values.get("segments") or []),
        "translations_json": _json(values.get("translations") or []),
        "history_json": _json(history),
        "version": version,
        "error_message": values.get("error") or "",
    }
    database.save_task_snapshot(task_id, snapshot)
    # 术语表单独成表，方便前端编辑；整表替换保证与 state 完全一致。
    database.replace_glossary(task_id, values.get("glossary") or [])
    database.append_event(task_id, stage, effective_status, message, version)

    event = {
        "task_id": task_id,
        "stage": stage,
        "status": effective_status,
        "progress": progress,
        "message": message or _last_note(values),
        "version": version,
    }
    if review:
        # review 只在有数据时附带，SSE 前端可直接展示 4 维评分。
        event["review"] = review
    publish(task_id, event)
    return event


def _fail(task_id: int, error: str) -> None:
    """异常兜底：写失败原因（旧版 mark_failed 丢 error 的 bug 已修）并广播。"""
    database.mark_failed(task_id, error)
    publish(
        task_id,
        {
            "task_id": task_id,
            "stage": "failed",
            "status": "failed",
            "progress": 100,
            "message": f"任务失败：{error}",
        },
    )


# ============================================
# 核心执行
# ============================================

def _stream(graph, graph_input, graph_config: dict, task_id: int) -> None:
    """
    执行图并逐步落库。

    graph_input 三种形态：
    - dict：全新执行；
    - None：从最近 checkpoint 续跑（进程重启恢复）；
    - Command(resume=...)：人工审核恢复。
    """
    started_at = datetime.now()
    interrupted = False

    try:
        for chunk in graph.stream(graph_input, graph_config, stream_mode="updates"):
            elapsed = (datetime.now() - started_at).total_seconds()
            if elapsed > config.TASK_TIMEOUT_SECONDS:
                raise TimeoutError(f"任务执行超过 {config.TASK_TIMEOUT_SECONDS}s 软超时")

            # interrupt() 暂停时 stream 会产出 {"__interrupt__": (...)}，此时还没进入 human_review 节点。
            if "__interrupt__" in chunk:
                interrupted = True
                break

            snapshot = graph.get_state(graph_config)
            values = snapshot.values or {}
            _persist(task_id, values, message=_last_note(values))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[runner] 任务 #{task_id} 执行失败: {error}")
        traceback.print_exc()
        _fail(task_id, error)
        return

    if interrupted:
        # 暂停在 human_review：更新为 waiting_human，前端看到后弹出人工审核面板。
        snapshot = graph.get_state(graph_config)
        values = snapshot.values or {}
        _persist(task_id, values, status="waiting_human", message="等待人工审核（或在 /resume 前可编辑术语表）")
        database.update_task_runtime(task_id, status="waiting_human")
        return

    # 正常结束：以 finalize 写入的 status 为准。
    snapshot = graph.get_state(graph_config)
    values = snapshot.values or {}
    status = str(values.get("status") or "failed")
    if status not in ("done", "failed"):
        status = "failed"
    message = "任务完成" if status == "done" else (values.get("error") or "任务失败")
    _persist(task_id, values, status=status, message=message)


def execute_task(task_id: int, mode: str = "start") -> None:
    """
    后台执行入口（由 ThreadPoolExecutor 调用）。

    mode:
      - start：全新执行（会先清掉该 thread 的旧 checkpoint，避免和上一次中断残留混合）；
      - restart：/retry 重置后重新执行；
      - resume：进程重启后从 checkpoint 续跑。
    """
    if not _acquire(task_id):
        # 同一任务已在执行：直接跳过，不抛异常。
        # 原因：本函数在线程池里跑，抛出去没人接；重复调度的正确行为是"幂等地什么都不做"，
        # 前端看到的是任务继续往下跑（而不是报错）。
        print(f"[runner] 任务 #{task_id} 已在执行中，跳过重复调度")
        return

    try:
        task = database.get_task(task_id)
        if task is None:
            print(f"[runner] 任务 #{task_id} 不存在，跳过")
            return

        if mode == "resume":
            graph = get_workflow()
            graph_config = {"configurable": {"thread_id": str(task_id)}}
            snapshot = graph.get_state(graph_config)
            values = snapshot.values or {}
            if not values:
                _fail(task_id, "服务重启后未找到 checkpoint 快照，无法续跑；请点击重试")
                return
            if not snapshot.next:
                # 图已经执行完，只是 DB 快照没来得及写：按最终 state 补写一次。
                status = str(values.get("status") or "failed")
                _persist(
                    task_id,
                    values,
                    status=status if status in ("done", "failed") else "failed",
                    message="恢复：图已执行完毕，补写最终状态",
                )
                return
            print(f"[runner] 任务 #{task_id} 从 checkpoint 续跑（next={snapshot.next}）")
            _stream(graph, None, graph_config, task_id)
            return

        # start / restart：清掉旧 thread 再从头跑，thread_id 固定为 task_id。
        try:
            from core.checkpointer import get_checkpointer

            get_checkpointer().delete_thread(str(task_id))
        except Exception as exc:
            print(f"[runner] 清理任务 #{task_id} 旧 checkpoint 失败（继续执行）: {exc}")

        database.update_task_runtime(task_id, status="running", stage="init", progress=1, error="")
        graph = get_workflow()
        graph_config = {"configurable": {"thread_id": str(task_id)}}
        is_restart = mode == "restart" or int(task.get("version") or 0) > 0
        initial_state = build_initial_state(task, restart=is_restart)
        _persist(task_id, initial_state, message="任务开始执行")
        _stream(graph, initial_state, graph_config, task_id)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[runner] 任务 #{task_id} 启动失败: {error}")
        traceback.print_exc()
        _fail(task_id, error)
    finally:
        _release(task_id)


def resume_task(task_id: int, decision: dict) -> None:
    """人工审核恢复：用 Command(resume=decision) 让 human_review 节点继续执行。"""
    if not _acquire(task_id):
        print(f"[runner] 任务 #{task_id} 正在执行，忽略重复 resume")
        return
    try:
        graph = get_workflow()
        graph_config = {"configurable": {"thread_id": str(task_id)}}
        snapshot = graph.get_state(graph_config)
        if not snapshot.next:
            # 可能已经被另一路 resume 处理完，幂等返回。
            print(f"[runner] 任务 #{task_id} 没有待恢复的暂停点，忽略 resume")
            return
        database.update_task_runtime(task_id, status="running", stage="human_review", progress=90, error="")
        publish(
            task_id,
            {
                "task_id": task_id,
                "stage": "human_review",
                "status": "running",
                "progress": 90,
                "message": "已收到人工审核结果，继续执行",
            },
        )
        _stream(graph, Command(resume=decision), graph_config, task_id)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[runner] 任务 #{task_id} resume 失败: {error}")
        traceback.print_exc()
        _fail(task_id, error)
    finally:
        _release(task_id)


# ============================================
# 启动恢复
# ============================================

def recover_interrupted_tasks(executor: Executor) -> list[int]:
    """
    服务启动时扫描「上次没跑完」的任务并自动续跑。

    - running：交给 resume 模式，读取 checkpoint 的 next 继续；
    - pending：从入参重新跑（入参在 DB，重复执行无副作用）。
    waiting_human 不在扫描范围：它在等人类操作，重启后应该继续等，而不是自动通过。
    """
    try:
        tasks = database.list_tasks_by_status(["running", "pending"])
    except Exception as exc:
        print(f"[runner] 扫描未完成任务失败（数据库不可用？）: {exc}")
        return []

    recovered: list[int] = []
    for task in tasks:
        task_id = int(task["id"])
        mode = "resume" if task.get("status") == "running" else "start"
        database.append_event(
            task_id,
            stage="recovery",
            status="running",
            message=f"服务重启：检测到 {task.get('status')} 任务，自动{'续跑' if mode == 'resume' else '重新开始'}",
            version=int(task.get("version") or 0),
        )
        executor.submit(execute_task, task_id, mode)
        recovered.append(task_id)
        print(f"[runner] 恢复任务 #{task_id}（mode={mode}）")

    if recovered:
        print(f"[runner] 共恢复 {len(recovered)} 个未完成任务: {recovered}")
    return recovered