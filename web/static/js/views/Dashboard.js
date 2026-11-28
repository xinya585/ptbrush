import { defineComponent, computed, reactive, ref, onMounted, onUnmounted } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api.js';
import { formatBytes, formatSpeed, formatGiB, truncate } from '../utils.js';

export default defineComponent({
    name: 'Dashboard',
    setup() {
        const stats = reactive({
            upspeed: 0,
            dlspeed: 0,
            up_total: 0,
            dl_total: 0,
            free_space: 0,
            timestamp: '',
        });
        const torrents = ref([]);
        const totalActive = ref(0);
        const statsConfigured = ref(true);
        const statsStale = ref(false);
        const health = ref({ ok: true, missing: [] });

        async function loadStats() {
            try {
                const data = await api.get('/api/stats');
                statsConfigured.value = data.configured !== false;
                statsStale.value = !!data.stale;
                Object.assign(stats, data.status);
                torrents.value = data.torrents;
                totalActive.value = data.total_active;
            } catch (e) {
                console.error('加载统计数据出错:', e);
            }
        }

        // ---- 免费期监控：剩余多久、是否已结束 ----
        function parseFreeTime(str) {
            if (!str) return null;
            const d = new Date(String(str).replace(' ', 'T'));
            return isNaN(d.getTime()) ? null : d;
        }

        function freeLeftOf(row) {
            const d = parseFreeTime(row && row.free_end_time);
            if (!d) return { text: '—', cls: '' };
            const mins = Math.round((d.getTime() - Date.now()) / 60000);
            if (mins <= 0) return { text: '已结束', cls: 'free-over' };
            const cls = mins <= 60 ? 'free-urgent' : (mins <= 180 ? 'free-soon' : '');
            if (mins < 60) return { text: mins + ' 分钟', cls };
            const hours = mins / 60;
            return { text: (hours >= 10 ? hours.toFixed(0) : hours.toFixed(1)) + ' 小时', cls };
        }

        // 表里用短时间（今年不带年份），省宽度
        function freeEndShort(str) {
            if (!str) return '-';
            const d = parseFreeTime(str);
            if (!d) return str;
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const dd = String(d.getDate()).padStart(2, '0');
            const hh = String(d.getHours()).padStart(2, '0');
            const mi = String(d.getMinutes()).padStart(2, '0');
            const prefix = d.getFullYear() === new Date().getFullYear() ? '' : d.getFullYear() + '-';
            return prefix + mm + '-' + dd + ' ' + hh + ':' + mi;
        }

        // 快到期排前面，已结束的沉底
        const sortedTorrents = computed(() => {
            const rows = (torrents.value || []).slice();
            rows.sort((a, b) => {
                const da = parseFreeTime(a.free_end_time);
                const db = parseFreeTime(b.free_end_time);
                const ta = da ? da.getTime() : Number.MAX_SAFE_INTEGER;
                const tb = db ? db.getTime() : Number.MAX_SAFE_INTEGER;
                const oa = da && da.getTime() <= Date.now();
                const ob = db && db.getTime() <= Date.now();
                if (oa !== ob) return oa ? 1 : -1;
                return ta - tb;
            });
            return rows;
        });

        // 空转种：免费期快结束却零上传，手动清理（先预览再确认）
        const idleLoading = ref(false);

        async function cleanIdle() {
            idleLoading.value = true;
            try {
                const res = await api.get('/api/brush/idle?hours=1');
                const items = (res && res.items) || [];
                if (!items.length) {
                    ElMessage.success('没有需要清理的空转种（免费期剩余 1 小时内且零上传的种子为 0）');
                    return;
                }
                const lines = items.slice(0, 12).map(function (it) {
                    return it.site + ' ｜ ' + it.name.slice(0, 44) + ' ｜ 免费截止 ' + it.free_end_time;
                });
                const more = items.length > 12 ? '\n…另有 ' + (items.length - 12) + ' 个' : '';
                await ElMessageBox.confirm(
                    '将删除 ' + items.length + ' 个空转种（种子 + 文件一起删）：\n\n' + lines.join('\n') + more
                        + '\n\n这些种子免费期不足 1 小时且一点没上传，留着只会占磁盘和下载位。',
                    '清理空转种',
                    { type: 'warning', confirmButtonText: '全部删除', cancelButtonText: '取消' },
                );
                const done = await api.post('/api/brush/idle/clean', { hashes: items.map(function (it) { return it.hash; }) });
                ElMessage.success('已删除 ' + (done.deleted || 0) + ' 个空转种');
                loadStats();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || '清理失败');
            } finally {
                idleLoading.value = false;
            }
        }

        async function loadHealth() {
            try {
                health.value = await api.get('/api/config/health');
            } catch (e) {
                // 接口缺失或未登录时不阻断 Dashboard
            }
        }

        // 正在执行的刷流站点（账号级数据，30 秒刷新）
        const brushSites = ref([]);
        const brushSitesUpdated = ref('');

        async function loadBrushSites() {
            try {
                const res = await api.get('/api/brush/sites');
                brushSites.value = (res && res.sites) || [];
                brushSitesUpdated.value = (res && res.updated_at) || '';
            } catch (e) {
                brushSites.value = [];
            }
        }

        function ratioText(row) {
            if (!row || !row.ok || !row.ratio) return '—';
            return Number(row.ratio).toFixed(2);
        }

        function bytesOrDash(row, field) {
            if (!row || !row.ok) return '—';
            return formatBytes(row[field] || 0) || '0 B';
        }

        // 今日增量：站点没给账号数据时是 —，给了就是具体数值（0 也写成 0 B，别留空让人以为没数据）
        function todayText(row, field) {
            if (!row || !row.ok) return '—';
            const v = row[field];
            if (v === null || v === undefined) return '—';
            return formatBytes(v) || '0 B';
        }

        // 最近一轮刷流诊断（每轮刷流都写一份，提前退出也会写）
        const report = ref(null);

        // 删种记录：最近删掉了哪些种子、为什么删
        const deletions = ref([]);

        async function loadDeletions() {
            try {
                const res = await api.get('/api/brush/deletions?limit=60');
                deletions.value = (res && res.deletions) || [];
            } catch (e) {
                deletions.value = [];
            }
        }

        async function loadReport() {
            try {
                const res = await api.get('/api/brush/report');
                report.value = (res && res.available) ? res.report : null;
            } catch (e) {
                report.value = null;
            }
        }

        // 一轮刷流诊断：卡片上只放「结论」，细节全塞进 details，条数多了在卡内滚动
        const reportView = computed(() => {
            const r = report.value;
            if (!r || !r.time) return null;
            const num = (v) => Number(v || 0);
            const added = num(r.added);
            const cand = num(r.candidates);
            const checked = num(r.checked);
            const filtered = num(r.filtered);
            const result = r.result || 'completed';

            let tone = 'ok';
            let head = '';
            let tail = '';
            if (result === 'full') {
                tone = 'warn';
                head = r.message || '下载位已占满';
            } else if (result === 'low_disk') {
                tone = 'bad';
                head = r.message || '下载盘空间不足';
            } else if (result === 'site_ratio_blocked') {
                tone = 'warn';
                head = r.message || '站点分享率已达目标，暂停新增';
            } else if (added > 0) {
                head = '本轮新增 ' + added + ' 个种子';
                tail = '候选 ' + cand + ' 个（实际检查 ' + checked + ' 个，其中没取到种子的不算）';
            } else {
                tone = 'warn';
                head = '本轮没有加到种子';
                tail = '候选 ' + cand + ' 个（实际检查 ' + checked + ' 个）';
            }

            // 明细行：键值对，空值不占行
            const details = [];
            const push = (k, v) => {
                if (v === undefined || v === null || v === '' || v === '—') return;
                details.push({ k, v });
            };

            // 只有真正跑到选种那一步，候选/筛种统计才有意义；
            // 下载位满、磁盘不足这类提前退出只报原因和现场状态。
            if (result === 'completed') {
                push('候选池', cand + ' 个');
                push('实际检查', checked + ' 个');
                push('按选种条件筛掉', filtered + ' 个');
                const reasonEntries = Object.entries(r.reasons || {}).sort((a, b) => b[1] - a[1]);
                if (reasonEntries.length) {
                    push('筛种原因', reasonEntries.map(([k, v]) => k + ' ' + v + ' 个').join(' ｜ '));
                }
                const picked = Array.isArray(r.selected) ? r.selected : [];
                if (picked.length) {
                    push('本轮选中', picked.map((i) => i.name + '（' + i.site + '，' + i.size_gb + ' GiB）').join(' ｜ '));
                }
                const addedItems = Array.isArray(r.added_items) ? r.added_items : [];
                if (addedItems.length) {
                    push('成功加种', addedItems.map((i) => i.name + '（' + i.site + '，' + i.size_gb + ' GiB）').join(' ｜ '));
                }
            }
            if (r.uncompleted !== undefined && r.limit !== undefined) {
                push('下载位', r.uncompleted + ' / ' + r.limit + ' 个');
            }
            if (r.disk_free_gb !== undefined) {
                push('下载盘剩余', r.disk_free_gb + ' GiB');
            }
            if (r.min_disk_gb !== undefined) {
                push('空间下限', r.min_disk_gb + ' GiB');
            }
            const sites = Array.isArray(r.target_site_names) && r.target_site_names.length
                ? r.target_site_names
                : (Array.isArray(r.target_sites) ? r.target_sites : []);
            if (targetSitesForReport(sites)) {
                push('参与站点', targetSitesForReport(sites));
            }
            if (Array.isArray(r.promotions) && r.promotions.length) {
                push('促销要求', r.promotions.join(' / '));
            }
            if (r.size_range) {
                push('体积区间', r.size_range + ' GiB');
            }
            if (r.min_free_hours !== undefined) {
                push('免费剩余门槛', r.min_free_hours + ' 小时');
            }
            if (r.exclude_hr !== undefined) {
                push('排除 H&R', r.exclude_hr ? '是' : '否');
            }

            return { tone, head, tail, time: r.time, details };
        });

        function targetSitesForReport(sites) {
            if (!sites.length) return '';
            const list = sites.slice(0, 8).join('、');
            return sites.length > 8 ? list + ' 等 ' + sites.length + ' 个站' : list;
        }

        let statsTimer = null;
        let reportTimer = null;
        let sitesTimer = null;
        let delTimer = null;

        onMounted(() => {
            loadStats();
            loadHealth();
            loadReport();
            loadBrushSites();
            loadDeletions();
            statsTimer = setInterval(loadStats, 10000);
            reportTimer = setInterval(loadReport, 60000);
            sitesTimer = setInterval(loadBrushSites, 600000); // 10 分钟
            delTimer = setInterval(loadDeletions, 60000); // 1 分钟
        });

        onUnmounted(() => {
            if (statsTimer) clearInterval(statsTimer);
            if (reportTimer) clearInterval(reportTimer);
            if (sitesTimer) clearInterval(sitesTimer);
            if (delTimer) clearInterval(delTimer);
        });

        return {
            stats,
            statsConfigured,
            statsStale,
            torrents,
            totalActive,
            health,
            reportView,
            formatBytes,
            formatSpeed,
            formatGiB,
            truncate,
            sortedTorrents,
            freeLeftOf,
            freeEndShort,
            cleanIdle,
            idleLoading,
            brushSites,
            brushSitesUpdated,
            deletions,
            ratioText,
            bytesOrDash,
            todayText,
        };
    },
    template: `
        <div class="sites-page">
            <div class="sites-header">
                <div>
                    <div class="sites-title">仪表盘</div>
                    <div class="sites-sub">
                        <template v-if="!statsConfigured">还没有配置下载器，仪表盘不显示运行状态。</template>
                        <template v-else>
                            默认下载器的实时状态（每 10 秒刷新）<template v-if="statsStale"> · 最新采样 {{ stats.timestamp || '未知' }}</template>
                        </template>
                    </div>
                </div>
            </div>
            <el-alert
                v-if="!health.ok && health.missing?.length"
                type="warning"
                show-icon
                :closable="false"
                style="margin-bottom:16px;"
                title="配置不完整，刷流功能可能无法工作">
                <template #default>
                    <div>缺失字段：<el-tag v-for="m in health.missing" :key="m" type="warning" size="small" style="margin-right:4px;">{{ m }}</el-tag></div>
                    <router-link to="/start" style="color:#e6a23c;text-decoration:underline;">前往快速开始 →</router-link>
                </template>
            </el-alert>

            <el-alert
                v-if="statsConfigured && statsStale"
                type="warning"
                show-icon
                :closable="false"
                style="margin-bottom:16px;"
                title="下载器状态已过期"
                :description="'最近一次采样是 ' + (stats.timestamp || '未知时间') + '，已超过 15 分钟没有更新。连接恢复后会自动刷新。'" />

            <el-row v-if="statsConfigured" :gutter="16">
                <el-col :xs="12" :sm="6" v-for="card in [
                    { label: '上传速度', unit: 'MiB/s', val: formatSpeed(stats.upspeed) },
                    { label: '下载速度', unit: 'MiB/s', val: formatSpeed(stats.dlspeed) },
                    { label: '活跃种子数', unit: '', val: totalActive },
                    { label: '剩余空间', unit: 'GiB', val: formatGiB(stats.free_space) }
                ]" :key="card.label">
                    <el-card class="stat-card" shadow="hover">
                        <div class="stat-value">{{ card.val }}</div>
                        <div class="stat-label">
                            {{ card.label }}
                            <span class="speed-unit" v-if="card.unit">({{ card.unit }})</span>
                        </div>
                    </el-card>
                </el-col>
            </el-row>

            <el-card v-if="!statsConfigured" class="stat-card" shadow="hover" style="margin-bottom:16px;">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;">
                    <div>
                        <div class="stat-value" style="font-size:18px;">未配置下载器</div>
                        <div class="stat-label" style="margin-top:4px;">添加一个 qBittorrent 下载器后，这里会显示实时上传、下载、活跃种子和剩余空间。</div>
                    </div>
                    <router-link to="/downloader">
                        <el-button type="primary" plain>去下载器配置</el-button>
                    </router-link>
                </div>
            </el-card>

            <el-card class="mt-4 brush-sites" shadow="hover" style="margin-top:16px;">
                <template #header>
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <span>正在执行的刷流站点</span>
                        <span style="color:#909399;font-size:12px;">
                            每 10 分钟更新 ｜ 账号数据: {{ brushSitesUpdated || '-' }}
                        </span>
                    </div>
                </template>
                <el-table :data="brushSites" stripe size="small" class="brush-sites-table">
                    <el-table-column label="Logo" width="54" align="center">
                        <template #default="{ row }">
                            <img class="site-logo" :src="row.icon" :alt="row.name" loading="lazy" />
                        </template>
                    </el-table-column>
                    <el-table-column label="站名" width="210">
                        <template #default="{ row }">
                            <div class="site-name">{{ row.name }}</div>
                            <div class="site-domain">{{ row.domain }}</div>
                            <div v-if="row.note" class="site-note">{{ row.note }}</div>
                        </template>
                    </el-table-column>
                    <el-table-column label="分享率" width="92" align="right">
                        <template #default="{ row }"><span class="num">{{ ratioText(row) }}</span></template>
                    </el-table-column>
                    <el-table-column label="上传" width="116" align="right">
                        <template #default="{ row }"><span class="num">{{ bytesOrDash(row, 'upload') }}</span></template>
                    </el-table-column>
                    <el-table-column label="下载" width="116" align="right">
                        <template #default="{ row }"><span class="num">{{ bytesOrDash(row, 'download') }}</span></template>
                    </el-table-column>
                    <el-table-column label="做种数" width="80" align="right">
                        <template #default="{ row }"><span class="num">{{ row.ok ? row.seeding : '—' }}</span></template>
                    </el-table-column>
                    <el-table-column label="做种量" width="116" align="right">
                        <template #default="{ row }"><span class="num">{{ bytesOrDash(row, 'seeding_size') }}</span></template>
                    </el-table-column>
                    <el-table-column label="今日上传" width="126" align="right">
                        <template #default="{ row }">
                            <span class="today-up num">{{ todayText(row, 'today_up') }}</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="今日下载（计费）" width="126" align="right">
                        <template #default="{ row }">
                            <span class="today-dl num">{{ todayText(row, 'today_dl') }}</span>
                        </template>
                    </el-table-column>
                    <template #empty>
                        <span style="color:#909399;">没有参与刷流的站点（全部站点参与时这里会列出所有启用的站点）</span>
                    </template>
                </el-table>
                <div class="brush-mobile-list">
                    <div class="brush-mobile-item" v-for="row in brushSites" :key="'m-' + row.domain">
                        <div class="brush-mobile-head">
                            <img :src="row.icon" :alt="row.name" loading="lazy" />
                            <span class="brush-mobile-name">{{ row.name }}</span>
                            <span class="brush-mobile-domain">{{ row.domain }}</span>
                        </div>
                        <div class="brush-mobile-grid">
                            <div class="brush-mobile-kv"><span>分享率</span><span>{{ ratioText(row) }}</span></div>
                            <div class="brush-mobile-kv"><span>上传</span><span>{{ bytesOrDash(row, 'upload') }}</span></div>
                            <div class="brush-mobile-kv"><span>下载</span><span>{{ bytesOrDash(row, 'download') }}</span></div>
                            <div class="brush-mobile-kv"><span>做种数</span><span>{{ row.ok ? row.seeding : '—' }}</span></div>
                            <div class="brush-mobile-kv"><span>做种量</span><span>{{ bytesOrDash(row, 'seeding_size') }}</span></div>
                            <div class="brush-mobile-kv"><span>今日上传</span><span>{{ todayText(row, 'today_up') }}</span></div>
                            <div class="brush-mobile-kv"><span>今日下载</span><span>{{ todayText(row, 'today_dl') }}</span></div>
                        </div>
                    </div>
                    <div v-if="!brushSites.length" style="color:#909399;font-size:12px;">
                        没有参与刷流的站点（全部站点参与时这里会列出所有启用的站点）
                    </div>
                </div>
            </el-card>

            <div v-if="reportView" class="brush-report" :class="'tone-' + reportView.tone">
                <div class="brush-report-main">
                    <div class="brush-report-head">
                        刷流诊断 · 上一轮 <span class="brush-report-time">{{ reportView.time }}</span>
                    </div>
                    <div class="brush-report-line">
                        <strong>{{ reportView.head }}</strong>
                        <span v-if="reportView.tail"> ｜ {{ reportView.tail }}</span>
                    </div>
                    <div v-if="reportView.details.length" class="brush-report-detail">
                        <div class="brush-report-row" v-for="d in reportView.details" :key="d.k">
                            <span class="brush-report-k">{{ d.k }}</span>
                            <span class="brush-report-v">{{ d.v }}</span>
                        </div>
                    </div>
                </div>
            </div>

            <el-card class="mt-4 brush-deletions" shadow="hover" style="margin-top:16px;">
                <template #header>
                    <div class="brush-deletions-head">
                        <span>删种记录 · 最近 {{ deletions.length }} 条</span>
                        <span class="brush-deletions-tip">删除时种子和已下载的文件一起清掉</span>
                    </div>
                </template>
                <el-table
                    :data="deletions"
                    stripe
                    size="small"
                    max-height="300"
                    empty-text="最近没有删过种子"
                    class="brush-deletions-table">
                    <el-table-column label="时间" width="128">
                        <template #default="{ row }">{{ row.time }}</template>
                    </el-table-column>
                    <el-table-column label="站点" width="96">
                        <template #default="{ row }">{{ row.site || '—' }}</template>
                    </el-table-column>
                    <el-table-column label="种子" min-width="300">
                        <template #default="{ row }">
                            <div class="brush-del-name" :title="row.name">{{ row.name }}</div>
                            <div class="brush-del-sub">
                                体积 {{ formatGiB(row.size) }} GiB
                                <span v-if="row.ratio">　分享率 {{ Number(row.ratio).toFixed(2) }}</span>
                                <span v-if="row.seeding_hours">　已做种 {{ row.seeding_hours }} 小时</span>
                            </div>
                        </template>
                    </el-table-column>
                    <el-table-column label="删种原因" min-width="320">
                        <template #default="{ row }">
                            <div class="brush-del-reason">
                                <el-tag size="small" :type="row.rule === '促销失效' ? 'warning' : 'info'" effect="plain">
                                    {{ row.rule || '自动' }}
                                </el-tag>
                                <span class="brush-del-reason-text">{{ row.reason }}</span>
                            </div>
                            <div v-if="row.detail" class="brush-del-sub">{{ row.detail }}</div>
                        </template>
                    </el-table-column>
                </el-table>
            </el-card>

            <el-card class="mt-4" shadow="hover" style="margin-top:16px;">
                <template #header>
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <span>刷流种子 · 按免费到期排序</span>
                        <div style="display:flex;align-items:center;gap:12px;">
                            <el-button size="small" plain :loading="idleLoading" @click="cleanIdle">清理空转种</el-button>
                            <span style="color:#909399;font-size:12px;">
                                最后更新: {{ stats.timestamp || '-' }}
                            </span>
                        </div>
                    </div>
                </template>
                <el-table :data="sortedTorrents" stripe size="small">
                    <el-table-column label="名称" min-width="300">
                        <template #default="{ row }">
                            <el-tooltip :content="row.name" placement="top" :show-after="200">
                                <div class="torrent-name">{{ row.name }}</div>
                            </el-tooltip>
                        </template>
                    </el-table-column>
                    <el-table-column prop="site" label="站点" width="88" />
                    <el-table-column label="上传速度" width="96" align="right">
                        <template #default="{ row }">{{ formatSpeed(row.upspeed) }} MiB/s</template>
                    </el-table-column>
                    <el-table-column label="下载速度" width="96" align="right">
                        <template #default="{ row }">{{ formatSpeed(row.dlspeed) }} MiB/s</template>
                    </el-table-column>
                    <el-table-column label="已上传" width="92" align="right">
                        <template #default="{ row }">{{ formatBytes(row.up_total) }}</template>
                    </el-table-column>
                    <el-table-column label="已下载" width="92" align="right">
                        <template #default="{ row }">{{ formatBytes(row.dl_total) }}</template>
                    </el-table-column>
                    <el-table-column label="剩余免费" width="98" align="center">
                        <template #default="{ row }">
                            <span :class="['free-left', freeLeftOf(row).cls]">{{ freeLeftOf(row).text }}</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="免费截止" width="140" align="center">
                        <template #default="{ row }">{{ freeEndShort(row.free_end_time) }}</template>
                    </el-table-column>
                    <el-table-column prop="score" label="分数" width="66" align="right" />
                    <template #empty>
                        <span style="color:#909399;">暂无刷流种子</span>
                    </template>
                </el-table>
            </el-card>
        </div>
    `,
});
