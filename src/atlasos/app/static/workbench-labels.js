// Presentation only. API values, filters and CSS classes still use the recorded codes.
const zhState = (() => {
  const labels = new Map(Object.entries({
    SUBMITTED: "已提交", RUNNING: "执行中", STARTED: "已开始", COMPLETED: "已完成",
    SUCCEEDED: "已成功", FAILED: "运行失败", INTERRUPTED: "执行中断", CANCELLED: "已取消",
    WAITING_APPROVAL: "等待审批", PENDING: "待处理", SANDBOX_LOST: "Sandbox 已断开",
    CLEANUP_UNRESOLVED: "清理状态未确认", RECOVERY_REQUIRED: "需要恢复",
    RECOVERY_UNCERTAIN: "恢复结果不确定", CANCEL_UNAVAILABLE: "无法取消",
    REVIEW_PENDING: "等待审查", APPLIED: "已应用", REJECTED: "已拒绝", CONFLICT: "工作区冲突",
    APPLY_FAILED: "应用失败", UNKNOWN: "未知", UNAVAILABLE: "不可用", AVAILABLE: "可用",
    UNKNOWN_TERMINAL_STATE: "终态未知", NOT_CONFIGURED: "未配置", NOT_OBSERVED: "未观测到",
    NOT_APPLICABLE: "不适用", NOT_RUN: "未执行", PASS: "通过", FAIL: "失败",
    ERROR: "错误", OK: "正常", ALLOW: "允许", DENY: "拒绝", REFUSED: "已拒绝",
    PRESENT: "有记录", MISSING: "缺失", COMPLETE: "完整", PARTIAL: "部分记录",
    MINIMAL: "最低限度记录", NONE: "无", YES: "是", NO: "否", TRUE: "是", FALSE: "否",
    DETERMINISTIC: "确定性脚本", SCRIPTED: "脚本", LIVE_MODEL: "真实模型",
    NOT_COMPLETED: "未完成", COMPLETED_UNVERIFIED: "已完成，未验证",
    NO_EXECUTION_EVIDENCE: "无执行证据", NOT_DELIVERED: "未交付",
    DELIVERED_AND_VERIFIED: "已交付并验证", NO_BATCH_RECORDED: "未记录 Delegation 批次",
    ADDED: "新增", MODIFIED: "修改", DELETED: "删除"
  }));
  return value => labels.get(String(value ?? "").toUpperCase()) ?? value;
})();
