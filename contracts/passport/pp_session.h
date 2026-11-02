// main/pp_session.h —— 对话回合状态机(纯逻辑,host 可测)。
//
// 时间:所有 now_ms 须非零(0 表示"未设置"),按 uint32 回绕安全比较。
//
// 单一拥有者:应用任务独占一个 pp_session_t,按键回调、网关链路、音频 worker 都只往它的
// 事件队列投递事件;本模块把事件转成"动作"交还给应用任务去执行。这样"取消"和"陈旧数据过滤"
// 只有一个裁决点:
//   * turn / turn_live 决定哪个回合的服务端消息被接受;
//   * PP_ACT_SET_GATE 把当前回合号发布给链路任务与音频 worker(0 = 没有活动回合),
//     它们据此丢弃旧回合的下行音频(见 pp_pcmq 的代号机制)。
#pragma once

#include "pp_input.h"
#include "pp_proto.h"

#include <stdbool.h>
#include <stdint.h>

#define PP_MIN_UTTERANCE_MS      300     // 短于此视为误触:发 cancel,回合在 ASR 之前终止。
                                         // 注意:录音从 PRESS 起就在流式上行,松手才能判定时长,
                                         // 所以不能声称"没有任何字节发出"
// THINKING 的截止时间按阶段计,并由网关的每阶段预算(ASR 25s + 大脑 25s + TTS 首音 15s = 65s,见
// docs/development/voice-protocol.md "Deadlines")加余量得到:设备只在网关沉默/失联时才超时,
// 网关预算内的有效工作不会被设备抢先放弃;网关自己的超时会以 asr_timeout/brain_timeout/tts_timeout
// 如实到达。不做任何自动重跑。每收到一个阶段进展(transcript -> reply_text)就换下一阶段的预算。
#define PP_THINK_ASR_MS          28000   // 结束上传 -> 收到最终 transcript(网关 ASR 预算 25s:真机上 SenseVoice 偶有 >12s 的长尾)
#define PP_THINK_BRAIN_MS        28000   // transcript -> reply_text
#define PP_THINK_TTS_MS          18000   // reply_text -> audio_start
#define PP_THINK_TOTAL_MS        75000   // 总上限/兜底(> 25+25+15=65 秒的服务端预算,且 >= 28+28+18=74 秒的设备阶段之和)
#define PP_SPEAK_STALL_MS        10000   // 播放中无下行进展的上限
#define PP_ERROR_SHOW_MS         3500
#define PP_SLEEP_AFTER_MS        45000
#define PP_VOLUME_OVERLAY_MS     1200
#define PP_VOLUME_STEP           10
#define PP_NOTE_SHOW_MS          1800    // "太短了"/"已打断"一类轻提示
#define PP_PLEASED_SHOW_MS       1600    // motion-spec.json pleased.duration_ms
#define PP_GENTLE_SHOW_MS        2000    // motion-spec.json gentle.duration_ms

typedef enum {
    PP_ST_BOOT = 0,
    PP_ST_IDLE,
    PP_ST_RECORDING,
    PP_ST_THINKING,
    PP_ST_SPEAKING,
    PP_ST_ERROR,
    PP_ST_OFFLINE,
    PP_ST_SLEEP,
    PP_ST_SETTINGS,
    PP_ST__COUNT,
} pp_state_t;

// 链路状态由 Wi-Fi/WebSocket 层上报;只有 READY 才允许开始录音。
typedef enum {
    PP_LINK_NO_CONFIG = 0,
    PP_LINK_WIFI_CONNECTING,
    PP_LINK_WIFI_DOWN,
    PP_LINK_TIME_SYNC,         // 已联网,正在校准时钟(证书有效期校验需要真实时间)
    PP_LINK_SERVER_CONNECTING,
    PP_LINK_AUTH_FAILED,
    PP_LINK_READY,
} pp_link_t;

typedef enum {
    PP_NOTE_NONE = 0,
    PP_NOTE_TOO_SHORT,
    PP_NOTE_INTERRUPTED,
    PP_NOTE_LIMIT,         // 20 秒到点自动提交
} pp_note_t;

typedef enum {
    PP_FACE_IDLE = 0,
    PP_FACE_LISTENING,
    PP_FACE_THINKING,
    PP_FACE_SPEAKING,
    PP_FACE_PLEASED,
    PP_FACE_GENTLE,
    PP_FACE_OFFLINE,       // 也用于"困惑"
    PP_FACE_SLEEPING,
    PP_FACE__COUNT,
} pp_face_t;

typedef enum {
    PP_ACT_NONE = 0,
    PP_ACT_SET_GATE,       // turn = 当前回合号,0 = 无活动回合(丢弃一切下行/上行旧数据)
    PP_ACT_MIC_START,      // turn
    PP_ACT_MIC_STOP,
    PP_ACT_SEND_START,     // turn
    PP_ACT_SEND_END,       // turn(字节数由链路任务按实际发送量填写)
    PP_ACT_SEND_CANCEL,    // turn
    PP_ACT_PLAY_BEGIN,     // turn
    PP_ACT_PLAY_STOP,      // 立即停止写 I2S 并丢弃队列(音频 worker 在下一个分块边界响应)
    PP_ACT_SET_VOLUME,     // arg = 0..100
    PP_ACT_SCREEN,         // arg = 1 亮屏,0 调暗
    PP_ACT_REDRAW,         // UI 需要按 pp_session_view() 刷新
} pp_act_kind_t;

typedef struct {
    pp_act_kind_t kind;
    uint32_t turn;
    uint32_t arg;
} pp_action_t;

#define PP_MAX_ACTIONS 10
typedef struct {
    uint8_t n;
    pp_action_t a[PP_MAX_ACTIONS];
} pp_actions_t;

// 工作线程回报的本地事件。
typedef enum {
    PP_EVT_PLAYBACK_DONE = 1,   // turn:该回合音频已实际播完(含 DMA 排空)
    PP_EVT_AUDIO_PROGRESS,      // turn:下行有进展(链路任务节流上报)
    PP_EVT_UPLINK_OVERFLOW,     // turn:上行队列满
    PP_EVT_PLAYBACK_OVERFLOW,   // turn:下行队列满且超时
    PP_EVT_MIC_FAIL,
    PP_EVT_PLAYBACK_FAIL,
    PP_EVT_SEND_FAIL,           // turn:发送失败/链路写超时
} pp_evt_kind_t;

typedef struct {
    pp_state_t state;
    pp_link_t link;
    uint32_t next_turn;          // 下一个要分配的回合号(从 1 开始)
    uint32_t turn;               // 当前/最近回合号
    bool turn_live;              // 当前回合仍可接受服务端消息
    bool audio_started;
    bool audio_end_seen;
    bool ignore_talk_up;         // 唤醒/退出设置/离线按下产生的 OK,其抬起须被吞掉
    bool after_interrupt;        // 当前录音由打断旧回合而来(太短时提示"已打断")
    uint32_t state_since_ms;
    uint32_t rec_start_ms;
    uint32_t think_since_ms;     // 进入 THINKING 的时刻(总上限从此计)
    uint8_t think_stage;         // 0 等 transcript,1 等 reply_text,2 等 audio_start
    uint32_t last_activity_ms;
    uint32_t last_progress_ms;
    uint32_t overlay_until_ms;   // 音量浮层
    uint32_t note_until_ms;
    uint32_t transient_until_ms; // pleased/gentle 短暂姿态
    pp_face_t transient_face;
    pp_note_t note;
    pp_err_t err;
    pp_mood_t mood;
    uint8_t volume;              // 0..100
    bool has_transcript;
    bool has_reply;
    bool reply_trunc;
    uint32_t stale_dropped;      // 被门控丢弃的服务端消息数(诊断)
    char transcript[PP_TEXT_MAX];
    char reply[PP_TEXT_MAX];
} pp_session_t;

typedef struct {
    pp_face_t face;
    pp_state_t state;
    pp_note_t note;
    pp_err_t err;
    pp_link_t link;
    bool mic_active;             // 录音中:UI 必须显示录音标识与电平
    bool volume_overlay;
    bool show_transcript;
    bool show_reply;
    bool reply_trunc;
    uint8_t volume;
} pp_view_t;

void pp_session_init(pp_session_t *s, uint8_t volume, uint32_t now_ms);
void pp_session_input(pp_session_t *s, pp_input_t in, uint32_t now_ms, pp_actions_t *out);
void pp_session_link(pp_session_t *s, pp_link_t link, uint32_t now_ms, pp_actions_t *out);
void pp_session_server(pp_session_t *s, const pp_srv_msg_t *msg, uint32_t now_ms, pp_actions_t *out);
void pp_session_event(pp_session_t *s, pp_evt_kind_t kind, uint32_t turn, uint32_t now_ms,
                      pp_actions_t *out);
void pp_session_tick(pp_session_t *s, uint32_t now_ms, pp_actions_t *out);
void pp_session_view(const pp_session_t *s, uint32_t now_ms, pp_view_t *out);
