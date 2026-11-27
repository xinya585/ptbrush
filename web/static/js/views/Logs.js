import { defineComponent, ref, onMounted, onBeforeUnmount, nextTick } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';

/**
 * 运行日志页。
 *
 * 读容器里 loguru 写的 ptbrush.log（跟 data 目录一起挂载），
 * 抓种、加种、清理、站点刷新这些动作都会落在里面。
 * 支持按级别 / 关键字过滤，也可以开自动刷新盯着看。
 */

const LEVELS = [
    { value: 'ALL', label: '全部级别' },
    { value: 'INFO', label: 'INFO' },
    { value: 'SUCCESS', label: 'SUCCESS' },
    { value: 'WARNING', label: 'WARNING' },
    { value: 'ERROR', label: 'ERROR' },
];

const LINE_OPTIONS = [100, 300, 500, 1000, 2000];

function humanSize(n) {
    const v = Number(n) || 0;
    if (v < 1024) return `${v} B`;
    if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
    return `${(v / 1024 / 1024).toFixed(2)} MB`;
}

export default defineComponent({
    name: 'Logs',
    setup() {
        const lines = ref(300);
        const level = ref('ALL');
        const keyword = ref('');
        const auto = ref(true);
        const loading = ref(false);
        const rows = ref([]);
        const meta = ref({});
        const viewBox = ref(null);
        let timer = null;

        const levels = LEVELS;
        const lineOptions = LINE_OPTIONS;

        const toolbarStyle = {
            background: '#fff',
            border: '1px solid #e4e7ed',
            borderRadius: '8px',
            padding: '12px 14px',
            marginBottom: '12px',
        };

        const logBoxStyle = {
            background: '#0b1020',
            border: '1px solid #1f2937',
            borderRadius: '8px',
            padding: '10px 0',
            maxHeight: 'calc(100vh - 300px)',
            minHeight: '260px',
            overflow: 'auto',
        };

        function levelColor(lv) {
            const map = {
                ERROR: '#ff6b6b',
                CRITICAL: '#ff4d4f',
                WARNING: '#ffb020',
                SUCCESS: '#35c46f',
                DEBUG: '#8b95a8',
                TRACE: '#8b95a8',
            };
            return map[lv] || '#d7dde8';
        }

        function scrollToBottom() {
            nextTick(() => {
                const el = viewBox.value;
                if (el) el.scrollTop = el.scrollHeight;
            });
        }

        async function load(scroll = true) {
            loading.value = true;
            try {
                const query = new URLSearchParams({
                    lines: String(lines.value),
                    level: level.value || '',
                    q: keyword.value || '',
                });
                const res = await api.get(`/api/logs?${query.toString()}`);
                rows.value = (res && res.rows) || [];
                meta.value = res || {};
                if (scroll) scrollToBottom();
            } catch (e) {
                ElMessage.error(e.message || '读取日志失败');
            } finally {
                loading.value = false;
            }
        }

        onMounted(() => {
            load(true);
            timer = setInterval(() => {
                if (auto.value && !loading.value) load(false);
            }, 8000);
        });

        onBeforeUnmount(() => {
            if (timer) clearInterval(timer);
            timer = null;
        });

        return {
            lines, level, keyword, auto, loading, rows, meta, viewBox,
            levels, lineOptions, toolbarStyle, logBoxStyle,
            load, levelColor, humanSize,
        };
    },
    template: `
        <div class="sites-page" v-loading="loading">
            <div class="sites-header">
                <div>
                    <div class="sites-title">日志</div>
                    <div class="sites-sub">
                        每 8 秒自动刷新
                    </div>
                </div>
                <div class="sites-actions">
                    <el-switch v-model="auto" active-text="自动刷新" />
                    <el-button :loading="loading" @click="load(true)">刷新</el-button>
                </div>
            </div>

            <div :style="toolbarStyle">
                <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                    <span style="font-size:13px;color:#606266;">显示行数</span>
                    <el-select v-model="lines" style="width:112px;" @change="load(true)">
                        <el-option v-for="n in lineOptions" :key="n" :label="n + ' 行'" :value="n" />
                    </el-select>
                    <el-select v-model="level" style="width:132px;" @change="load(true)">
                        <el-option v-for="l in levels" :key="l.value" :label="l.label" :value="l.value" />
                    </el-select>
                    <el-input
                        v-model="keyword"
                        placeholder="搜索关键字：站点名 / 种子名 / 错误信息"
                        clearable
                        style="width:280px;"
                        @keyup.enter="load(true)"
                        @clear="load(true)" />
                    <el-button @click="load(true)">查询</el-button>
                </div>
                <div style="font-size:12px;color:#909399;margin-top:8px;">
                    文件 {{ meta.file || '—' }} · {{ humanSize(meta.size) }} · 更新于 {{ meta.updated_at || '—' }}
                    · 命中 {{ meta.matched || 0 }} 行，当前显示最近 {{ rows.length }} 行
                </div>
            </div>

            <div ref="viewBox" :style="logBoxStyle">
                <div v-if="!rows.length" style="color:#8b95a8;padding:16px;">
                    {{ meta.message || '没有匹配的日志行' }}
                </div>
                <div
                    v-for="(row, i) in rows"
                    :key="i"
                    :style="{
                        color: levelColor(row.level),
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-all',
                        fontFamily: 'Consolas, Courier New, monospace',
                        fontSize: '12.5px',
                        lineHeight: '1.55',
                        padding: '0 12px',
                    }">{{ row.text }}</div>
            </div>
        </div>
    `,
});

