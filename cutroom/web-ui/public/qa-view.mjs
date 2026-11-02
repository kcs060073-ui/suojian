const CHECK_LABELS = Object.freeze({
  material_authorization: "素材授权待核对",
  true_peak: "声音峰值超标",
  clipping: "声音可能削波",
  freeze: "画面疑似卡顿",
  black_frame: "疑似黑画面",
  silence: "疑似静音",
  dependency: "镜头素材已入库",
  repeated_shot: "镜头无重复",
  packaging_gate: "包装方案已确认",
  privacy: "隐私标记已核对",
  material_fingerprint: "素材内容一致",
  material_scope: "素材来源符合范围",
  video_stream: "视频轨道正常",
  video_evidence: "视频证据已读取",
  video_codec: "视频编码正常",
  pixel_aspect_ratio: "像素比例正常",
  rotation: "画面方向正常",
  display_aspect_ratio: "竖屏比例正常",
  resolution: "分辨率达标",
  aspect_ratio: "画幅比例达标",
  frame_rate: "帧率达标",
  audio_stream: "音频轨道正常",
  duration: "视频时长已核对",
  subtitle_timing: "字幕时间正常",
  subtitle_lines: "字幕行数正常",
  subtitle_safe_area: "字幕位置安全",
  subtitle_evidence: "字幕证据已核对",
  loudness_integrated: "整体响度正常",
});

export function describeQaIssue(issue = {}) {
  const check = String(issue.check || "");
  const detail = String(issue.detail || "").trim();
  const title = CHECK_LABELS[check] || "质检项目";
  if (check === "material_authorization") {
    const count = (detail.match(/镜头素材授权未明确通过/g) || []).length;
    return {
      title,
      summary: count ? `${count} 个镜头素材需要核对商业使用授权。` : "请核对本片镜头素材的商业使用授权。",
      technical: detail,
      check,
    };
  }
  return { title, summary: detail || "没有补充说明。", technical: detail, check };
}

export function qaIssueSeekSeconds(issue = {}) {
  const value = issue.startSeconds;
  if (value === null || value === undefined || value === "") return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}
