// RGB Sync: reports Geometry Dash state to the RGB Sync desktop app.
//
// One JSON message per UDP datagram to 127.0.0.1:47831 (format: docs/gd-protocol.md).
// This mod only observes the game. It never changes gameplay and touches nothing
// but that one loopback socket.

#include <Geode/Geode.hpp>
#include <Geode/modify/PauseLayer.hpp>
#include <Geode/modify/PlayLayer.hpp>

// Geode's precompiled headers already pull in windows.h and with it winsock.h, so
// winsock2.h would clash. Everything used below is in winsock.h.
#include <winsock.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <string>

using namespace geode::prelude;

namespace rgb {
    using Clock = std::chrono::steady_clock;

    constexpr u_short kPort = 47831;
    constexpr auto kStateInterval = std::chrono::milliseconds(33); // about 30 Hz
    constexpr size_t kMaxDatagram = 511;                           // protocol: under 512 bytes
    constexpr size_t kMaxNameChars = 64;

    SOCKET g_socket = INVALID_SOCKET;
    sockaddr_in g_target{};
    bool g_ready = false;
    bool g_failed = false;
    Clock::time_point g_lastState{};
    bool g_deathSent = false;    // one death per attempt
    bool g_completeSent = false; // one complete per attempt

    // ---- networking -------------------------------------------------------

    // Fire and forget: lazily creates a non-blocking UDP socket, ignores every error.
    void post(std::string const& json) {
        if (g_failed || json.size() > kMaxDatagram) return;
        if (!g_ready) {
            WSADATA wsa;
            if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
                g_failed = true;
                return;
            }
            g_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
            if (g_socket == INVALID_SOCKET) {
                g_failed = true;
                return;
            }
            u_long nonBlocking = 1;
            ioctlsocket(g_socket, FIONBIO, &nonBlocking);
            g_target.sin_family = AF_INET;
            g_target.sin_port = htons(kPort);
            g_target.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            g_ready = true;
        }
        sendto(g_socket, json.data(), static_cast<int>(json.size()), 0,
               reinterpret_cast<sockaddr const*>(&g_target), sizeof(g_target));
    }

    // ---- JSON helpers -----------------------------------------------------

    char const* tf(bool value) { return value ? "true" : "false"; }

    float finite(float value) { return std::isfinite(value) ? value : 0.f; }

    // JSON string body for a level name: at most 64 characters, quotes and
    // backslashes escaped, control characters become spaces and non-ASCII bytes
    // become '?', so the output is always valid UTF-8 and one byte per character.
    std::string escapeName(std::string const& name) {
        std::string out;
        for (size_t i = 0; i < name.size() && i < kMaxNameChars; ++i) {
            auto c = static_cast<unsigned char>(name[i]);
            if (c == '"' || c == '\\') {
                out += '\\';
                out += static_cast<char>(c);
            } else if (c < 0x20 || c == 0x7f) {
                out += ' ';
            } else if (c >= 0x80) {
                out += '?';
            } else {
                out += static_cast<char>(c);
            }
        }
        return out;
    }

    // `extra` is empty or starts with a comma, e.g. ,"pct":12.3,"new_best":true
    void sendEvent(char const* type, std::string const& extra = "") {
        post(fmt::format(R"({{"v":1,"t":"{}"{}}})", type, extra));
    }

    // ---- reading the game -------------------------------------------------

    char const* modeOf(PlayerObject* p) {
        if (p->m_isShip) return "ship";
        if (p->m_isBall) return "ball";
        if (p->m_isBird) return "ufo";
        if (p->m_isDart) return "wave";
        if (p->m_isRobot) return "robot";
        if (p->m_isSpider) return "spider";
        if (p->m_isSwing) return "swing";
        return "cube";
    }

    char const* difficultyOf(GJGameLevel* level) {
        if (level->m_demon.value() > 0) return "demon";
        if (level->m_autoLevel) return "auto";
        switch (level->m_stars.value()) {
            case 1: return "auto";
            case 2: return "easy";
            case 3: return "normal";
            case 4: case 5: return "hard";
            case 6: case 7: return "harder";
            case 8: case 9: return "insane";
            case 10: return "demon";
            default: return "unrated";
        }
    }

    // Progress 0-100. Platformer levels have no percent.
    float percentOf(PlayLayer* pl) {
        if (pl->m_isPlatformer) return 0.f;
        return std::clamp(finite(pl->getCurrentPercent()), 0.f, 100.f);
    }

    // Stored best (whole percent) for the current mode, practice or normal.
    // The normal best is an obfuscated "seed value"; value() decodes it.
    int bestOf(PlayLayer* pl) {
        if (pl->m_isPlatformer || !pl->m_level) return 0;
        return pl->m_isPracticeMode ? pl->m_level->m_practicePercent
                                    : pl->m_level->m_normalPercent.value();
    }

    // Would reaching `percent` now beat the stored best? Must be asked BEFORE the
    // original destroyPlayer/levelComplete runs, because those save the new best.
    bool beatsBest(PlayLayer* pl, int percent) {
        return !pl->m_isPlatformer && !pl->m_isTestMode && percent > bestOf(pl);
    }

    // ---- accent color -----------------------------------------------------
    //
    // "The level's most prominent vivid color right now." Every color channel gets a
    // score = vividness x log2(1 + usage), where usage is how many objects in view use
    // the channel (plus a fixed weight for the background, ground and line channels,
    // which fill the screen without being objects). Channels nothing visible uses
    // score 0, so a vivid but invisible channel can never be picked.
    //
    // Read-only: it reads GJBaseGameLayer::m_activeObjects (the objects the game
    // currently has in view), each object's GJSpriteColor ids, and calls
    // GJEffectManager::colorExists / activeColorForIndex (plain lookups). Nothing is
    // allocated: all tables are fixed arrays. Objects are counted about 4.5 times a
    // second (at most kMaxCounted per pass); the channel choice runs about 12.5 times
    // a second and the result is reused between scans.

    constexpr int kAccentSlots = 1010;   // channel ids 0..1009 index the arrays below
    constexpr size_t kColorSlots = 1102; // GJEffectManager keeps color slots 0..1101
    constexpr auto kAccentScanInterval = std::chrono::milliseconds(80);  // about 12.5 Hz
    constexpr auto kUsageScanInterval = std::chrono::milliseconds(220);  // about 4.5 Hz
    constexpr size_t kMaxCounted = 2000;  // objects looked at per counting pass
    constexpr float kVividMin = 0.35f;    // saturation x value below this never counts
    constexpr int kChangeDelta = 8;       // RGB component move that counts as "changed"
    constexpr auto kBoostWindow = std::chrono::milliseconds(1000);
    constexpr float kBoost = 1.5f;         // score multiplier for a channel that just changed
    constexpr float kSwitchFactor = 1.25f; // a challenger must beat the current pick by 25%
    constexpr auto kMinHold = std::chrono::milliseconds(500); // no switch sooner than this
    // Fixed usage added to channels that cover the screen without being objects.
    constexpr int kUsageBackground = 600; // 1000
    constexpr int kUsageGround = 120;     // 1001, 1009
    constexpr int kUsageLine = 12;        // 1002, 1003

    ccColor3B g_prevColor[kAccentSlots];
    bool g_prevValid[kAccentSlots];
    bool g_everChanged[kAccentSlots];
    Clock::time_point g_changedAt[kAccentSlots];
    int g_usage[kAccentSlots]; // in-view objects per channel, from the last counting pass
    Clock::time_point g_lastAccentScan{};
    Clock::time_point g_lastUsageScan{};
    Clock::time_point g_lastSwitch{};
    bool g_hasSwitched = false;
    int g_accentChannel = -1; // -1: nothing vivid
    ccColor3B g_accentColor{0, 0, 0};

    // Called from PlayLayer::init (a new level load), not on every attempt.
    void resetAccent() {
        for (int i = 0; i < kAccentSlots; ++i) {
            g_prevColor[i] = ccColor3B{0, 0, 0};
            g_prevValid[i] = false;
            g_everChanged[i] = false;
            g_changedAt[i] = Clock::time_point{};
            g_usage[i] = 0;
        }
        g_lastAccentScan = Clock::time_point{};
        g_lastUsageScan = Clock::time_point{};
        g_lastSwitch = Clock::time_point{};
        g_hasSwitched = false;
        g_accentChannel = -1;
        g_accentColor = ccColor3B{0, 0, 0};
    }

    // Saturation x value (HSV), 0-1. Black, grey and white score 0 or low.
    float vividness(ccColor3B c) {
        int mx = std::max({int(c.r), int(c.g), int(c.b)});
        int mn = std::min({int(c.r), int(c.g), int(c.b)});
        if (mx == 0) return 0.f;
        float saturation = float(mx - mn) / float(mx);
        float value = float(mx) / 255.f;
        return saturation * value;
    }

    bool moved(ccColor3B a, ccColor3B b) {
        return std::abs(int(a.r) - int(b.r)) >= kChangeDelta ||
               std::abs(int(a.g) - int(b.g)) >= kChangeDelta ||
               std::abs(int(a.b) - int(b.b)) >= kChangeDelta;
    }

    // Channels worth looking at: 1..999 (user channels) plus the drawn special ones:
    // 1000 BG, 1001 ground, 1002 line, 1003 3DL, 1004 object, 1009 ground 2.
    // Skipped: 0 (white), 1005/1006 (player colors), 1007/1008 (derived), 1010/1011 (fixed).
    bool scanned(int ch) {
        return (ch >= 1 && ch <= 1004) || ch == 1009;
    }

    // The channel a GJSpriteColor really uses: its own id, or the object's default
    // channel when the id is 0 (same rule as GJSpriteColor::getColorMode).
    int channelOf(GJSpriteColor const* color) {
        if (!color) return 0;
        return color->m_colorID != 0 ? color->m_colorID : color->m_defaultColorID;
    }

    void countChannel(GJSpriteColor const* color) {
        int ch = channelOf(color);
        if (ch >= 1 && ch < kAccentSlots) ++g_usage[ch];
    }

    // Counts how many of the objects the game currently has in view use each channel.
    // m_activeObjectsCount says how many entries of m_activeObjects are live, so it is
    // clamped to the vector's real size and every pointer is null-checked. More than
    // kMaxCounted objects are sampled with a stride and scaled back up.
    void countUsage(GJBaseGameLayer* layer) {
        for (int i = 0; i < kAccentSlots; ++i) g_usage[i] = 0;
        if (!layer) return;

        auto const& objects = layer->m_activeObjects;
        int liveCount = layer->m_activeObjectsCount;
        size_t n = liveCount > 0 ? std::min(static_cast<size_t>(liveCount), objects.size()) : 0;
        if (n == 0) return;

        size_t stride = n > kMaxCounted ? (n + kMaxCounted - 1) / kMaxCounted : 1;
        for (size_t i = 0; i < n; i += stride) {
            GameObject* obj = objects[i];
            if (!obj) continue;
            // Triggers, hidden objects and toggled-off groups draw nothing.
            if (obj->m_isTrigger || obj->m_isHide || obj->m_isGroupDisabled) continue;
            countChannel(obj->m_baseColor);   // may be null
            countChannel(obj->m_detailColor); // null for objects without a detail color
        }
        if (stride > 1) {
            for (int i = 0; i < kAccentSlots; ++i) {
                g_usage[i] = static_cast<int>(std::min<long long>(
                    static_cast<long long>(g_usage[i]) * static_cast<long long>(stride), 1000000LL));
            }
        }
    }

    // How much of the screen a channel covers: its object count plus, for the channels
    // that fill the screen by themselves, a fixed weight.
    int usageOf(int ch) {
        int usage = g_usage[ch];
        if (ch == 1000) usage += kUsageBackground;
        else if (ch == 1001 || ch == 1009) usage += kUsageGround;
        else if (ch == 1002 || ch == 1003) usage += kUsageLine;
        return usage;
    }

    void scanAccent(GJEffectManager* em, Clock::time_point now) {
        // Without the "which channels exist" table, report nothing rather than guess.
        if (!em || em->m_colorActionVector.size() < kColorSlots) {
            g_accentChannel = -1;
            return;
        }

        int bestCh = -1; // highest score overall
        float bestScore = 0.f;
        ccColor3B bestColor{0, 0, 0};
        float currentScore = -1.f; // the current choice, if it still qualifies
        ccColor3B currentColor{0, 0, 0};

        for (int ch = 1; ch <= 1009; ++ch) {
            if (!scanned(ch)) continue;
            if (!em->colorExists(ch)) { // unused channel: nothing to read
                g_prevValid[ch] = false;
                continue;
            }
            ccColor3B c = em->activeColorForIndex(ch);

            if (g_prevValid[ch] && moved(c, g_prevColor[ch])) {
                g_changedAt[ch] = now;
                g_everChanged[ch] = true;
            }
            g_prevColor[ch] = c;
            g_prevValid[ch] = true;

            int usage = usageOf(ch);
            if (usage <= 0) continue; // nothing visible uses this channel
            float vivid = vividness(c);
            if (vivid < kVividMin) continue;

            float score = vivid * std::log2(1.f + static_cast<float>(usage));
            if (g_everChanged[ch] && now - g_changedAt[ch] <= kBoostWindow) score *= kBoost;

            if (ch == g_accentChannel) {
                currentScore = score;
                currentColor = c;
            }
            if (score > bestScore) {
                bestScore = score;
                bestCh = ch;
                bestColor = c;
            }
        }

        if (bestCh < 0) { // nothing qualifies
            g_accentChannel = -1;
            return;
        }
        if (currentScore < 0.f) { // no current pick, or it stopped qualifying: take the best
            g_accentChannel = bestCh;
            g_accentColor = bestColor;
            g_lastSwitch = now;
            g_hasSwitched = true;
            return;
        }
        bool held = g_hasSwitched && now - g_lastSwitch < kMinHold;
        if (bestCh != g_accentChannel && !held && bestScore > currentScore * kSwitchFactor) {
            g_accentChannel = bestCh;
            g_accentColor = bestColor;
            g_lastSwitch = now;
            g_hasSwitched = true;
        } else {
            g_accentColor = currentColor; // stay on the current choice, with its fresh color
        }
    }

    // `paused` is passed in because the pause menu has its own tick (see RgbPauseLayer).
    void sendState(PlayLayer* pl, bool paused) {
        auto p1 = pl->m_player1; // dual mode: report player 1
        if (!p1) return;

        auto now = Clock::now();
        if (now - g_lastState < kStateInterval) return;
        g_lastState = now;

        // Background color channel 1000.
        ccColor3B bg{0, 0, 0};
        if (pl->m_effectManager) bg = pl->m_effectManager->activeColorForIndex(1000);

        // Music levels used by GD's pulse triggers; only updated while metering is on.
        // Sent raw (the app auto-scales them), limited to 0-16 for safety.
        float pulse = 0.f, pulse2 = 0.f, pulse3 = 0.f;
        if (auto audio = FMODAudioEngine::get(); audio && audio->m_metering) {
            pulse = std::clamp(finite(audio->m_pulse1), 0.f, 16.f);
            pulse2 = std::clamp(finite(audio->m_pulse2), 0.f, 16.f);
            pulse3 = std::clamp(finite(audio->m_pulse3), 0.f, 16.f);
        }

        // Accent color: objects counted about 4.5 times a second, channels rescanned
        // about 12 times a second, results reused in between.
        if (now - g_lastUsageScan >= kUsageScanInterval) {
            g_lastUsageScan = now;
            countUsage(pl);
        }
        if (now - g_lastAccentScan >= kAccentScanInterval) {
            g_lastAccentScan = now;
            scanAccent(pl->m_effectManager, now);
        }
        std::string accent = "null";
        if (g_accentChannel >= 0) {
            accent = fmt::format("[{},{},{}]", int(g_accentColor.r), int(g_accentColor.g),
                                 int(g_accentColor.b));
        }

        float speed = p1->m_playerSpeed > 0.f ? p1->m_playerSpeed : 1.f;
        ccColor3B c = p1->m_playerColor1;

        post(fmt::format(
            R"({{"v":1,"t":"state","pct":{:.1f},"best":{:.1f},"mode":"{}","flip":{},"speed":{:.2f},)"
            R"("bg":[{},{},{}],"p1":[{},{},{}],"accent":{},"pulse":{:.3f},"pulse2":{:.3f},"pulse3":{:.3f},)"
            R"("practice":{},"paused":{}}})",
            percentOf(pl), static_cast<float>(bestOf(pl)), modeOf(p1), tf(p1->m_isUpsideDown), finite(speed),
            int(bg.r), int(bg.g), int(bg.b), int(c.r), int(c.g), int(c.b), accent,
            pulse, pulse2, pulse3, tf(pl->m_isPracticeMode), tf(paused)));
    }
}

class $modify(RgbPlayLayer, PlayLayer) {
    bool init(GJGameLevel* level, bool dontCreateObjects) {
        if (!PlayLayer::init(level, dontCreateObjects)) return false;
        rgb::g_deathSent = false;
        rgb::g_completeSent = false;
        rgb::resetAccent();
        if (level) {
            rgb::sendEvent("level_start", fmt::format(
                R"(,"name":"{}","stars":{},"difficulty":"{}","platformer":{})",
                rgb::escapeName(level->m_levelName), level->m_stars.value(),
                rgb::difficultyOf(level), rgb::tf(level->isPlatformer())));
        }
        return true;
    }

    void postUpdate(float dt) {
        PlayLayer::postUpdate(dt);
        rgb::sendState(this, m_isPaused);
    }

    void destroyPlayer(PlayerObject* player, GameObject* object) {
        // GD calls this with an invisible anti-cheat spike at the start of every level.
        bool antiCheatProbe = object && object == m_anticheatSpike;

        // The original saves a new best, so decide new_best first.
        int percent = static_cast<int>(rgb::percentOf(this));
        bool newBest = rgb::beatsBest(this, percent);

        PlayLayer::destroyPlayer(player, object);

        // With noclip the original may leave the player alive: only report a real death,
        // and only once per attempt.
        if (antiCheatProbe || rgb::g_deathSent) return;
        bool dead = (m_player1 && m_player1->m_isDead) || (m_player2 && m_player2->m_isDead);
        if (!dead) return;
        rgb::g_deathSent = true;
        rgb::sendEvent("death", fmt::format(R"(,"pct":{:.1f},"new_best":{})",
                                            rgb::percentOf(this), rgb::tf(newBest)));
    }

    void resetLevel() {
        PlayLayer::resetLevel();
        rgb::g_deathSent = false;
        rgb::g_completeSent = false;
        rgb::sendEvent("restart");
    }

    void levelComplete() {
        bool newBest = rgb::beatsBest(this, 100);
        PlayLayer::levelComplete();
        if (rgb::g_completeSent) return;
        rgb::g_completeSent = true;
        rgb::sendEvent("complete", fmt::format(R"(,"new_best":{})", rgb::tf(newBest)));
    }

    CheckpointObject* markCheckpoint() {
        auto checkpoint = PlayLayer::markCheckpoint();
        if (checkpoint) rgb::sendEvent("checkpoint");
        return checkpoint;
    }

    void onQuit() {
        rgb::sendEvent("level_exit");
        PlayLayer::onQuit();
    }
};

// The game scheduler keeps running while the pause menu is open, but the level's own
// update does not, so the pause menu sends the state itself (otherwise the app would
// see "no state for 3 seconds" and think we left the level).
class $modify(RgbPauseLayer, PauseLayer) {
    void customSetup() {
        PauseLayer::customSetup();
        this->schedule(schedule_selector(RgbPauseLayer::rgbTick), 1.f / 30.f);
    }

    void rgbTick(float) {
        if (auto pl = PlayLayer::get()) rgb::sendState(pl, true);
    }
};
