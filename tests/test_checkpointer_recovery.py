"""
test_checkpointer_recovery.py - 断点恢复测试

模拟「任务跑到翻译节点时进程被 kill」：
1. 用 SqliteSaver 跑图，第一次翻译调用抛异常；
2. 换一条 sqlite 连接 + 重新 compile（等价于进程重启后重新加载图）；
3. graph.invoke(None, config) 应从最近的 checkpoint 续跑并最终完成。

这是项目二最核心的验收点：没有 checkpointer 时，进程一挂任务就永久卡在 running。
"""
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

import agent
import workflow


def _initial_state():
    return workflow.build_initial_state(
        {
            "id": 7,
            "source_text": "人工智能正在改变世界。",
            "source_lang": "中文",
            "target_lang": "English",
            "style": "",
            "domain": "",
            "enable_human_review": False,
        }
    )


def _pass_review(**kwargs):
    return {
        "fidelity": 95,
        "terminology": 95,
        "fluency": 92,
        "completeness": 95,
        "overall": 94.0,
        "issues": [],
        "needs_retranslation": False,
        "unavailable": False,
    }


def _patch_ok_agents(monkeypatch):
    monkeypatch.setattr(agent, "extract_terms", lambda **kwargs: [{"term": "人工智能", "translation": "AI", "note": ""}])
    monkeypatch.setattr(agent, "translate_segment", lambda **kwargs: "AI is changing the world.")
    monkeypatch.setattr(agent, "review_translation", _pass_review)


def test_resume_from_checkpoint_after_crash(tmp_path, monkeypatch):
    checkpoint_path = tmp_path / "checkpoints.db"
    thread_id = "recover-thread"
    graph_config = {"configurable": {"thread_id": thread_id}}

    call_count = {"translate": 0}

    def flaky_translate(**kwargs):
        call_count["translate"] += 1
        if call_count["translate"] == 1:
            raise RuntimeError("模拟进程在第一次翻译时被 kill")
        return "AI is changing the world."

    monkeypatch.setattr(agent, "extract_terms", lambda **kwargs: [{"term": "人工智能", "translation": "AI", "note": ""}])
    monkeypatch.setattr(agent, "translate_segment", flaky_translate)
    monkeypatch.setattr(agent, "review_translation", _pass_review)

    conn1 = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    conn2 = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    try:
        graph1 = workflow.build_workflow(checkpointer=SqliteSaver(conn1))

        with pytest.raises(RuntimeError, match="模拟进程"):
            graph1.invoke(_initial_state(), config=graph_config)

        # 崩溃后：checkpoint 里保留了「下一步是 translate」的状态
        snapshot = graph1.get_state(graph_config)
        assert snapshot.values, "崩溃后应该存在 checkpoint 快照"
        assert snapshot.values["stage"] == "translate"
        assert snapshot.next, "应该有未完成的下一步，供进程重启后续跑"
        assert "translate" in snapshot.next

        # ---- 模拟进程重启：新连接、新 saver、新 compile ----
        graph2 = workflow.build_workflow(checkpointer=SqliteSaver(conn2))
        snapshot2 = graph2.get_state(graph_config)
        assert snapshot2.values["stage"] == "translate"

        resumed = graph2.invoke(None, config=graph_config)

        assert resumed["status"] == "done"
        assert resumed["retry_count"] == 1  # 崩溃的那次翻译没有提交，续跑从第 1 次重新算
        assert call_count["translate"] == 2
        # 崩溃前的 history 仍然保留（累积型 channel）
        assert any("初始化" in item["note"] for item in resumed["history"])
        assert any(item["stage"] == "translate" for item in resumed["history"])
    finally:
        conn1.close()
        conn2.close()


def test_missing_checkpoint_reports_not_resumable(tmp_path):
    checkpoint_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    try:
        graph = workflow.build_workflow(checkpointer=SqliteSaver(conn))
        snapshot = graph.get_state({"configurable": {"thread_id": "never-started"}})

        # 新线程没有快照：runner 据此判定「无法续跑，请重试」，而不是盲目重跑
        assert snapshot.values == {}
        assert not snapshot.next
    finally:
        conn.close()