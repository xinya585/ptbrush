import { defineComponent, computed, ref, onMounted, onUnmounted } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api.js';
import CookieCloudCard from '../components/CookieCloudCard.js';

const TYPES = [
    { value: 'nexusphp', label: 'NexusPHP', hint: '填站点地址 + 浏览器 Cookie' },
    { value: 'mteam', label: 'M-Team', hint: '用 x-api-key 请求头鉴权' },
];

const STATUS_META = {
    ok: { label: '正常', color: '#22c55e' },
    error: { label: '异常', color: '#ef4444' },
    disabled: { label: '已停用', color: '#94a3b8' },
    unknown: { label: '待抓取', color: '#f59e0b' },
};

/** 从地址推导域名键，和后端 site_domain_of 保持同一套规则 */
function domainOf(url) {
    if (!url) return '';
    let host = '';
    try {
        host = new URL(String(url).includes('://') ? url : 'https://' + url).hostname.toLowerCase();
    } catch (e) {
        return '';
    }
    host = host.replace(/^www\./, '');
    const parts = host.split('.').filter(Boolean);
    if (!parts.length) return '';
    if (parts.length > 3) return parts.join('.');
    return parts.length >= 2 ? parts.slice(-2).join('.') : parts[0];
}

/** 字节 -> 人类可读，和后端 human_bytes 对齐 */
function humanBytes(n) {
    const v = Number(n) || 0;
    if (!v) return '';
    const units = [['PiB', 1024 ** 5], ['TiB', 1024 ** 4], ['GiB', 1024 ** 3], ['MiB', 1024 ** 2], ['KiB', 1024]];
    for (const [unit, size] of units) {
        if (v >= size) return `${(v / size).toFixed(3)} ${unit}`;
    }
    return `${Math.round(v)} B`;
}

function emptySite() {
    return {
        name: '',
        type: 'nexusphp',
        base_url: '',
        rss_url: '',
        cookie: '',
        api_key: '',
        token: '',
        headers: [],
        enabled: true,
        // 保存时是下载器别名；打开弹窗没文件名可用就落到第一个别名
        downloader: '',
        // 该站点专用的保存路径；留空则用所绑定下载器的默认路径
        save_path: '',
        proxy: false,
        public: false,
        note: '',
        user_agent: '',
        timeout: 15,
        limit_seconds: 0,
        limit_interval: 0,
        limit_count: 0,
    };
}

export default defineComponent({
    name: 'Sites',
    components: { CookieCloudCard },
    setup() {
        const sites = ref([]);
        const siteTypes = ref(TYPES);
        // 下载器别名列表：站点绑定哪个 qB 从这里选
        const downloaders = ref([]);
        const loading = ref(false);
        const refreshing = ref(false);
        // 站点配置库（pre-dessert-sites）每周一 04:10 自动同步一次：图标 + 适配变化
        const libReport = ref(null);
        const libLoading = ref(false);
        const refreshingKey = ref('');
        const saving = ref(false);
        const testing = ref(false);

        const modal = ref(false);
        const draft = ref(null);
        // 解析器类型不再手选：有 API Key 就是 M-Team，否则 NexusPHP（RSS 站仍按 RSS 地址）
        const isApiKeySite = computed(() => !!((draft.value && draft.value.api_key) || '').trim());
        const autoTypeLabel = computed(() => (isApiKeySite.value ? 'M-Team 接口（按 API Key）' : 'NexusPHP（Cookie / RSS）'));
        const oldDomain = ref('');
        const apiTab = ref('cookie');
        const limitPanel = ref(false);
        const pmIconFail = ref(false);

        const keyword = ref('');
        const statusFilter = ref('all');
        const sortKey = ref('name');
        const menuKey = ref('');

        const sortedSites = computed(() => {
            let arr = sites.value.slice();
            const kw = keyword.value.trim().toLowerCase();
            if (kw) {
                arr = arr.filter(s => (s.name || '').toLowerCase().includes(kw) || (s.domain || '').toLowerCase().includes(kw));
            }
            if (statusFilter.value !== 'all') {
                arr = arr.filter(s => s.status === statusFilter.value);
            }
            // 只保留两种排序：中文名称（中文排序规则，等同拼音序）、上传量
            const byName = (a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN');
            if (sortKey.value === 'upload') arr.sort((a, b) => (b.uploaded_bytes || 0) - (a.uploaded_bytes || 0) || byName(a, b));
            else arr.sort(byName);
            return arr;
        });

        const libChanged = computed(() => ((libReport.value && libReport.value.adapters_changed) || []).length);
        const libText = computed(() => {
            const r = libReport.value;
            if (!r) return '';
            const icons = r.icons || {};
            const total = (icons.filled || []).length + (icons.updated || []).length + (icons.same || 0);
            const stamp = String(r.time || '').slice(5, 16);
            let text = `站点库已同步 ${stamp} ｜ 图标 ${total} 张`;
            if (!r.ok) return `站点库同步失败（${stamp}）：${String(r.message || '').slice(0, 40)}`;
            if (libChanged.value) text += ` ｜ 适配变化 ${libChanged.value} 个，去日志看`;
            return text;
        });

        const counts = computed(() => {
            const total = sites.value.length;
            const ok = sites.value.filter(s => s.status === 'ok').length;
            const bad = sites.value.filter(s => s.status === 'error').length;
            return { total, ok, bad };
        });

        // 进度条分母：所有站点里最大的上传量（上传/下载共用一个分母，量级更直观）
        const maxUp = computed(() => Math.max(0, ...sites.value.map(s => Number(s.uploaded_bytes) || 0)));
        const maxDown = computed(() => Math.max(0, ...sites.value.map(s => Number(s.downloaded_bytes) || 0)));

        function pct(value, base) {
            const v = Number(value) || 0;
            if (!v || !base) return 0;
            return Math.max(3, Math.min(100, (v / base) * 100));
        }

        function barStyle(dir, value) {
            const base = dir === 'up' ? (maxUp.value || maxDown.value) : (maxUp.value || maxDown.value);
            const p = pct(value, base);
            const color = dir === 'up' ? '#1f6feb' : '#f5a623';
            return { background: `linear-gradient(to right, ${color} 0%, ${color} ${p}%, #f0f2f5 ${p}%, #f0f2f5 100%)` };
        }

        function statusMeta(site) {
            return STATUS_META[site.status] || STATUS_META.unknown;
        }

        function statusTitle(site) {
            if (site.status === 'disabled') return '已停用，不抓种也不刷流';
            if (site.status === 'ok') return `站点数据正常${site.updated_at ? '（' + site.updated_at + '）' : ''}`;
            if (site.status === 'error') return site.message || '抓取失败';
            return '还没有抓到账号数据，点「刷新」试一下';
        }

        function upText(site) {
            return site.uploaded || '—';
        }
        function downText(site) {
            return site.downloaded || '—';
        }
        function ratioText(site) {
            return site.ratio || '—';
        }
        function seedingText(site) {
            return site.seeding ? `${site.seeding}` : '0';
        }
        function initialOf(site) {
            return String(site.name || '?').trim().slice(0, 2).toUpperCase();
        }
        function iconBroken(site) {
            site._iconBroken = true;
        }
        function browse(site) {
            const url = site.base_url || site.rss_url;
            if (!url) {
                ElMessage.warning('这个站点没有填地址，无法打开');
                return;
            }
            window.open(url, '_blank');
        }

        function apiPath(key) {
            return `/api/sites/${encodeURIComponent(key)}`;
        }

        async function load() {
            loading.value = true;
            try {
                const data = await api.get('/api/sites');
                sites.value = data.sites || [];
            } catch (e) {
                ElMessage.error(e.message || '读取站点失败');
            } finally {
                loading.value = false;
            }
        }

        // 下载器别名：拿不到就只剩「主下载器」一个选项，不影响站点保存
        async function loadDownloaders() {
            try {
                const data = await api.get('/api/downloaders');
                downloaders.value = (data && data.downloaders) || [];
            } catch (e) {
                downloaders.value = [];
            }
        }

        async function loadSupport() {
            try {
                const data = await api.get('/api/sites/support');
                if (data.types && data.types.length) siteTypes.value = data.types;
            } catch (e) {
                /* 类型元数据拿不到就用内置的三种，不影响主流程 */
            }
        }

        // 全量刷新：走批量接口，抓完再拉一次列表（快照都写进库了）
        // mode: 'manual' 手动点按钮 / 'hour' 整点自动刷 / 'silent' 进页面补刷（不弹提示）
        async function refreshAll(mode) {
            refreshing.value = true;
            try {
                await api.get('/api/sites/overview?refresh=1');
                await load();
                markHourRefreshed();
                if (mode === 'hour') ElMessage.success('整点自动刷新：站点数据已更新');
                else if (!mode) ElMessage.success('站点数据已刷新');
            } catch (e) {
                ElMessage.error(e.message || '刷新失败');
            } finally {
                refreshing.value = false;
            }
        }

        // 整点刷新：页面开着就每到 xx:00:05 强制拉一遍全部站点（?refresh=1 会绕过缓存）
        function hourStartOf(ts) {
            return Math.floor(ts / 3600000) * 3600000;
        }

        // localStorage 在隐私模式/禁用存储时会抛错，这里包一层，坏了只影响补刷不影响页面
        const HOUR_KEY = 'ptbrush.sites.lastForceRefresh';
        function markHourRefreshed() {
            try { localStorage.setItem(HOUR_KEY, String(Date.now())); } catch (e) {}
        }
        function lastRefreshAt() {
            try { return Number(localStorage.getItem(HOUR_KEY) || 0) || 0; } catch (e) { return 0; }
        }

        let hourTimer = null;
        function scheduleHourlyRefresh() {
            if (hourTimer) clearTimeout(hourTimer);
            const now = new Date();
            const next = new Date(now.getTime());
            next.setHours(now.getHours() + 1, 0, 0, 0);
            // 留 5 秒缓冲，避免和整点的其它定时任务（刷流/删种巡检）撞在同一个瞬间
            const delay = Math.max(1000, next.getTime() - now.getTime() + 5000);
            hourTimer = setTimeout(async () => {
                hourTimer = null;
                if (!refreshing.value) await refreshAll('hour');
                scheduleHourlyRefresh();
            }, delay);
        }

        // 站点配置库：读上次同步结果；手动「立即更新」走同一个后台函数（约 3–16 秒）
        async function loadLib() {
            try {
                const data = await api.get('/api/sites/predessert');
                libReport.value = (data && data.report) || null;
            } catch (e) {
                libReport.value = null;
            }
        }

        async function syncLib() {
            if (libLoading.value) return;
            libLoading.value = true;
            try {
                await api.post('/api/sites/predessert/sync', {});
                await loadLib();
                await load();
                if (libChanged.value) ElMessage.warning(`站点库已更新：${libChanged.value} 个站点的解析规则有变化，具体看日志`);
                else ElMessage.success('站点库已更新：图标已同步，解析规则无变化');
            } catch (e) {
                ElMessage.error(e.message || '站点库更新失败');
            } finally {
                libLoading.value = false;
            }
        }

        // 进页面/切回标签页时，如果本小时还没强制刷过就补刷一次（浏览器把后台定时器节流了也能兜住）
        async function catchUpHourly() {
            const last = lastRefreshAt();
            if (last >= hourStartOf(Date.now())) return;
            if (refreshing.value) return;
            await refreshAll('silent');
        }

        // 标签页被挂起时定时器会被浏览器节流，回来时对一下时间，错过整点就补刷一次
        function onVisibility() {
            if (document.visibilityState !== 'visible') return;
            scheduleHourlyRefresh();
            catchUpHourly();
        }

        // 单站刷新：只打这一个站，不影响其它站
        async function refreshOne(site) {
            refreshingKey.value = site.domain;
            try {
                const res = await api.post(`${apiPath(site.domain)}/refresh`, {});
                if (res && res.site) {
                    const idx = sites.value.findIndex(s => s.domain === site.domain);
                    if (idx >= 0) sites.value[idx] = res.site;
                    if (res.site.status === 'ok') ElMessage.success(`${res.site.name} 数据已更新`);
                    else ElMessage.warning(`${res.site.name}：${res.site.message || '抓取失败'}`);
                }
            } catch (e) {
                ElMessage.error(e.message || '刷新失败');
            } finally {
                refreshingKey.value = '';
            }
        }

        function openEdit(site) {
            menuKey.value = '';
            oldDomain.value = site.domain || '';
            pmIconFail.value = false;
            const aliases = downloaders.value.map(d => d.alias);
            const bound = (site.downloader || '').trim();
            draft.value = {
                ...emptySite(),
                ...JSON.parse(JSON.stringify(site)),
                rss_url: site.rss_url || '',
                user_agent: site.user_agent || '',
                // 没绑过、或存的别名已不存在（改名/删除）时，落到第一个别名
                downloader: aliases.length ? (aliases.includes(bound) ? bound : aliases[0]) : bound,
            };
            apiTab.value = site.has_api_key && !site.has_cookie ? 'api' : 'cookie';
            limitPanel.value = !!(site.limit_seconds || site.limit_interval || site.limit_count);
            modal.value = true;
        }

        function openNew() {
            menuKey.value = '';
            oldDomain.value = '';
            pmIconFail.value = false;
            draft.value = {
                ...emptySite(),
                downloader: downloaders.value[0] ? downloaders.value[0].alias : '',
            };
            apiTab.value = 'cookie';
            limitPanel.value = false;
            modal.value = true;
        }

        function closeModal() {
            modal.value = false;
            draft.value = null;
            oldDomain.value = '';
        }

        function toggleMenu(site) {
            menuKey.value = menuKey.value === site.domain ? '' : site.domain;
        }

        function headerValue(key) {
            const h = ((draft.value && draft.value.headers) || []).find(x => (x.key || '').toLowerCase() === key.toLowerCase());
            return h ? h.value || '' : '';
        }

        function setHeaderValue(key, value) {
            if (!draft.value) return;
            const list = (draft.value.headers || []).map(h => ({ ...h }));
            const idx = list.findIndex(h => (h.key || '').toLowerCase() === key.toLowerCase());
            if (value) {
                if (idx >= 0) list[idx].value = value;
                else list.push({ key, value });
            } else if (idx >= 0) {
                list.splice(idx, 1);
            }
            draft.value.headers = list;
        }

        // 当前选中下载器的默认保存路径，用来给站点弹窗做占位提示
        const currentDownloaderDefaultPath = computed(() => {
            const alias = ((draft.value && draft.value.downloader) || '').trim();
            const found = (downloaders.value || []).find(d => (d.alias || '') === alias);
            return (found && found.save_path) || '';
        });
        const savePathPlaceholder = computed(() => {
            const fallback = currentDownloaderDefaultPath.value;
            return fallback ? `留空则用下载器默认：${fallback}` : '留空则用下载器的默认保存路径';
        });

        function shortHost(url) {
            return String(url || '')
                .replace(/^https?:\/\//, '')
                .replace(/\/$/, '')
                .split('/')[0];
        }

        function secretPlaceholder(value, fallback) {
            // 现在 Cookie / API Key / Token 都明文回显，占位符只做提示
            return fallback;
        }

        function resolveType(d) {
            const apiKey = ((d && d.api_key) || '').trim();
            if (apiKey) return 'mteam';
            const baseUrl = ((d && d.base_url) || '').trim();
            if (!baseUrl && ((d && d.rss_url) || '').trim()) return 'rss';
            return 'nexusphp';
        }

        function buildPayload() {
            const d = draft.value || {};
            const rssUrl = (d.rss_url || '').trim();
            return {
                old_domain: oldDomain.value,
                name: (d.name || '').trim(),
                type: resolveType(d),
                base_url: (d.base_url || '').trim(),
                enabled: d.enabled !== false,
                downloader: (d.downloader || '').trim(),
                save_path: (d.save_path || '').trim(),
                cookie: d.cookie || '',
                api_key: d.api_key || '',
                token: d.token || '',
                // proxy / public 已从界面下线，不上报，保存时沿用配置文件里的原值
                note: d.note || '',
                limit_seconds: Number(d.limit_seconds) || 0,
                limit_interval: Number(d.limit_interval) || 0,
                limit_count: Number(d.limit_count) || 0,
                headers: (d.headers || []).map(h => ({ key: h.key, value: h.value || '' })),
                options: {
                    rss_url: rssUrl,
                    user_agent: d.user_agent || '',
                    timeout: Number(d.timeout) || 0,
                },
            };
        }

        function validate() {
            const d = draft.value || {};
            const name = (d.name || '').trim();
            if (!name) {
                ElMessage.warning('站点名称不能为空');
                return false;
            }
            const type = resolveType(d);
            if (type === 'nexusphp' && !(d.base_url || '').trim()) {
                ElMessage.warning('NexusPHP 站必须填站点地址');
                return false;
            }
            if (type === 'rss' && !(d.rss_url || '').trim()) {
                ElMessage.warning('RSS 站必须填 RSS 地址');
                return false;
            }
            if (d.base_url && !domainOf(d.base_url)) {
                ElMessage.warning('站点地址格式不对，示例：https://pt.example.com');
                return false;
            }
            return true;
        }

        async function saveDraft() {
            if (!validate()) return;
            saving.value = true;
            try {
                const payload = buildPayload();
                let res;
                if (oldDomain.value) {
                    res = await api.put('/api/sites', payload);
                } else {
                    delete payload.old_domain;
                    res = await api.post('/api/sites', payload);
                }
                if (res && res.site) {
                    const idx = sites.value.findIndex(s => s.domain === (oldDomain.value || res.site.domain));
                    if (idx >= 0) sites.value[idx] = res.site;
                    else sites.value.push(res.site);
                }
                ElMessage.success(oldDomain.value ? '站点已保存' : `已新增站点「${payload.name}」`);
                closeModal();
                await load();
            } catch (e) {
                ElMessage.error(e.message || '保存失败');
            } finally {
                saving.value = false;
            }
        }

        async function testDraft() {
            if (!draft.value) return;
            testing.value = true;
            try {
                const res = await api.post('/api/sites/test', buildPayload());
                const r = (res && res.result) || {};
                if (res && res.ok) {
                    ElMessage.success(`连接正常：${r.username || '已登录'}，上传 ${r.uploaded || '—'}，下载 ${r.downloaded || '—'}`);
                } else {
                    ElMessage.error(r.message || '测试失败');
                }
            } catch (e) {
                ElMessage.error(e.message || '请求失败');
            } finally {
                testing.value = false;
            }
        }

        // 批量删除：一次删全部 / 只删最近一次刷新失败的站点
        const bulkDeleting = ref('');

        function escHtml(text) {
            return String(text == null ? '' : text)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }

        async function bulkDelete(mode) {
            const list = mode === 'all'
                ? sites.value.slice()
                : sites.value.filter(s => s.status === 'error');
            if (!list.length) {
                ElMessage.info(mode === 'all' ? '当前没有站点' : '没有刷新失败的站点');
                return;
            }
            const names = list.map(s => s.name || s.domain).join('、');
            const title = mode === 'all' ? '删除全部站点' : '删除无效站点';
            const tip = mode === 'all'
                ? `<div>将删除全部 <b>${list.length}</b> 个站点：</div>
                   <div style="margin-top:6px;color:#606266;">${escHtml(names)}</div>
                   <div style="margin-top:10px;color:#909399;font-size:12px;">站点配置、Cookie / API Key、参与刷流的引用一并清除；已下载的种子和历史统计保留。此操作不可撤销。</div>`
                : `<div>将删除最近一次刷新失败的 <b>${list.length}</b> 个站点：</div>
                   <div style="margin-top:6px;color:#606266;">${escHtml(names)}</div>
                   <div style="margin-top:10px;color:#909399;font-size:12px;">Cookie / API Key 与刷流引用一并清除；已下载的种子和历史统计保留。从没抓取过的站点不算无效，会保留。</div>`;
            try {
                await ElMessageBox.confirm(tip, title, {
                    type: 'warning',
                    dangerouslyUseHTMLString: true,
                    confirmButtonText: mode === 'all' ? '全部删除' : '删除',
                    cancelButtonText: '取消',
                });
            } catch (e) {
                return;
            }
            bulkDeleting.value = mode;
            try {
                const res = await api.post('/api/sites/bulk-delete', { mode });
                if (!res || res.ok === false) {
                    ElMessage.warning((res && res.message) || '没有删除任何站点');
                } else {
                    ElMessage.success(res.message || `已删除 ${list.length} 个站点`);
                }
                if (res && res.skipped_unknown) {
                    ElMessage.info(`另有 ${res.skipped_unknown} 个站点还没抓取过数据，未计入无效`);
                }
                await load();
            } catch (e) {
                ElMessage.error(e.message || '批量删除失败');
            } finally {
                bulkDeleting.value = '';
            }
        }

        function removeInvalid() {
            return bulkDelete('invalid');
        }

        function removeAll() {
            return bulkDelete('all');
        }

        async function removeSite(site) {
            menuKey.value = '';
            try {
                await ElMessageBox.confirm(
                    `删除站点「${site.name || site.domain}」？已下载的种子不会被清理，Cookie 会一起删掉。`,
                    '删除站点',
                    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
                );
            } catch (e) {
                return;
            }
            try {
                await api.delete(apiPath(site.domain));
                sites.value = sites.value.filter(s => s.domain !== site.domain);
                ElMessage.success(`已删除站点「${site.name}」`);
            } catch (e) {
                ElMessage.error(e.message || '删除失败');
            }
        }

        onMounted(() => {
            load();
            loadSupport();
            loadDownloaders();
            loadLib();
            document.addEventListener('click', () => { menuKey.value = ''; });
            scheduleHourlyRefresh();
            document.addEventListener('visibilitychange', onVisibility);
            // 页面挂在整点之外打开时，只要本小时还没刷过就先强制刷一遍
            catchUpHourly();
        });

        onUnmounted(() => {
            if (hourTimer) clearTimeout(hourTimer);
            document.removeEventListener('visibilitychange', onVisibility);
        });

        return {
            sites, siteTypes, isApiKeySite, autoTypeLabel, loading, refreshing, refreshingKey, saving, testing,
            modal, draft, oldDomain, apiTab, limitPanel, pmIconFail,
            keyword, statusFilter, sortKey, menuKey,
            sortedSites, counts, maxUp, maxDown,
            load, refreshAll, refreshOne, openEdit, openNew, closeModal, toggleMenu,
            libReport, libLoading, libText, libChanged, loadLib, syncLib,
            browse, statusMeta, statusTitle, upText, downText, ratioText, seedingText,
            initialOf, iconBroken, barStyle, domainOf, humanBytes,
            headerValue, setHeaderValue, secretPlaceholder, saveDraft, testDraft, removeSite,
            downloaders, shortHost, savePathPlaceholder,
            bulkDeleting, removeInvalid, removeAll,
};
    },
    template: `
        <div class="sites-page" v-loading="loading">
            <div class="sites-header">
                <div>
                    <div class="sites-title">站点配置</div>
                    <div class="sites-sub">
                        共 {{ counts.total }} 个站点 · 正常 {{ counts.ok }} 个
                        <span v-if="counts.bad" class="sites-sub-bad">· 异常 {{ counts.bad }} 个</span>
                    </div>
                    <div v-if="libText" class="sites-lib">
                        <span :class="{ 'sites-lib-warn': libChanged || (libReport && !libReport.ok) }">{{ libText }}</span>
                        <a href="#" @click.prevent="syncLib">{{ libLoading ? '更新中…' : '立即更新' }}</a>
                    </div>
                    <div v-else-if="libLoading" class="sites-lib">站点库同步中…</div>
                </div>
                <div class="sites-actions">
                    <el-input v-model="keyword" placeholder="搜索站点名 / 域名" clearable style="width:180px;" />
                    <el-select v-model="statusFilter" style="width:120px;">
                        <el-option value="all" label="全部状态" />
                        <el-option value="ok" label="正常" />
                        <el-option value="error" label="异常" />
                        <el-option value="unknown" label="待抓取" />
                    </el-select>
                    <el-select v-model="sortKey" style="width:152px;">
                        <el-option value="name" label="按名称（中文）" />
                        <el-option value="upload" label="按上传量" />
                    </el-select>
                    <el-button :loading="refreshing" title="每整点自动刷新一次，也可手动点" @click="refreshAll">刷新数据</el-button>
                    <el-button type="primary" @click="openNew">新增站点</el-button>
                    <el-button
                        plain
                        type="danger"
                        :disabled="!counts.bad"
                        :loading="bulkDeleting === 'invalid'"
                        title="只删最近一次刷新失败（异常）的站点；待抓取、已停用的保留"
                        @click="removeInvalid">删除无效站点</el-button>
                    <el-button
                        type="danger"
                        :disabled="!sites.length"
                        :loading="bulkDeleting === 'all'"
                        title="删除配置里的全部站点，不可撤销"
                        @click="removeAll">删除全部站点</el-button>
                </div>
            </div>

            <CookieCloudCard @changed="load" />

            <div v-if="!sites.length && !loading" class="sites-empty">
                <div class="sites-empty-title">还没有配置站点</div>
                <div class="sites-empty-sub">点右上角「新增站点」，填站点地址和 Cookie 就能开始刷流。</div>
                <el-button type="primary" plain @click="openNew">新增站点</el-button>
            </div>

            <div v-else class="pt-site-grid">
                <div
                    v-for="site in sortedSites"
                    :key="site.domain || site.name"
                    class="ptc"
                    :class="[ 'st-' + site.status, { 'is-off': !site.enabled } ]"
                    @click="openEdit(site)">
                    <div class="ptc-topline"></div>

                    <div class="ptc-actions" @click.stop>
                        <button class="ptc-action" title="浏览站点" @click="browse(site)">🌐</button>
                        <button
                            class="ptc-action"
                            :class="{ spinning: refreshingKey === site.domain }"
                            title="刷新这个站点"
                            @click="refreshOne(site)">⟳</button>
                        <button class="ptc-action" :class="{ active: menuKey === site.domain }" title="更多操作" @click="toggleMenu(site)">⋮</button>
                        <div v-if="menuKey === site.domain" class="ptc-menu" @click.stop>
                            <div class="ptc-menu-item" @click="openEdit(site)">编辑站点</div>
                            <div class="ptc-menu-item danger" @click="removeSite(site)">删除站点</div>
                        </div>
                    </div>

                    <div class="ptc-head">
                        <div class="ptc-icon">
                            <img
                                v-if="site.icon && !site._iconBroken"
                                :src="site.icon"
                                @error="iconBroken(site)" />
                            <span v-else class="ptc-icon-letter">{{ initialOf(site) }}</span>
                        </div>
                        <div class="ptc-title-wrap">
                            <div class="ptc-title">{{ site.name }}</div>
                            <div class="ptc-domain" :title="site.base_url || site.rss_url">{{ site.domain || site.base_url || '—' }}</div>
                        </div>
                        <span class="ptc-dot" :style="{ background: statusMeta(site).color }" :title="statusTitle(site)"></span>
                    </div>

                    <div class="ptc-metrics">
                        <div class="ptc-row">
                            <span class="ptc-arrow up">↑</span>
                            <span class="ptc-track" :style="barStyle('up', site.uploaded_bytes)"></span>
                            <span class="ptc-value">{{ upText(site) }}</span>
                        </div>
                        <div class="ptc-row">
                            <span class="ptc-arrow down">↓</span>
                            <span class="ptc-track" :style="barStyle('down', site.downloaded_bytes)"></span>
                            <span class="ptc-value">{{ downText(site) }}</span>
                        </div>
                    </div>

                    <div class="ptc-foot">
                        <span class="ptc-foot-item">分享率 {{ ratioText(site) }}</span>
                        <span class="ptc-foot-item">做种 {{ seedingText(site) }}</span>
                    </div>

                    <div v-if="site.message" class="ptc-msg" :class="{ 'ptc-msg-info': site.status !== 'error' }">
                        {{ site.message }}
                    </div>
                </div>
            </div>

            <div v-if="modal" class="pt-modal-mask" @click.self="closeModal">
                <div class="pt-modal" v-if="draft">
                    <div class="pm-head">
                        <div class="pm-head-left">
                            <div class="ptc-icon pm-icon">
                                <img
                                    v-if="oldDomain && !pmIconFail"
                                    :src="'/api/sites/icon?domain=' + oldDomain"
                                    @error="pmIconFail = true" />
                                <span v-else class="ptc-icon-letter">{{ (draft.name || '新').slice(0, 2).toUpperCase() }}</span>
                            </div>
                            <div>
                                <div class="pm-title">{{ oldDomain ? '编辑站点' : '新增站点' }}</div>
                                <div class="pm-sub">{{ draft.name || '未命名站点' }}</div>
                            </div>
                            <span class="pm-badge" v-if="domainOf(draft.base_url) || domainOf(draft.rss_url)">
                                域名键 {{ domainOf(draft.base_url) || domainOf(draft.rss_url) }}
                            </span>
                            <span class="pm-badge" v-if="isApiKeySite">M-Team</span>
                        </div>
                        <button class="pm-close" @click="closeModal">✕</button>
                    </div>

                    <div class="pm-section">
                        <div class="pm-grid">
                            <div class="pm-field">
                                <div class="pm-label">站点名称</div>
                                <el-input v-model="draft.name" placeholder="例如 家园 / 天空 / 馒头" />
                                <div class="pm-help">名称会写进种子名，建好后尽量别再改；改成中文也不影响任何已存数据。</div>
                            </div>
                            <div class="pm-field">
                                <div class="pm-label">解析方式（自动）</div>
                                <div class="pm-auto-type">{{ autoTypeLabel }}</div>
                                <div class="pm-help">不用手选：下面 API 页签填了 API Key 就走 M-Team 接口，否则按站点地址走 NexusPHP。</div>
                            </div>
                        </div>

                        <div class="pm-grid">
                            <div class="pm-field span2">
                                <div class="pm-label">站点地址</div>
                                <el-input v-model="draft.base_url" placeholder="https://pt.example.com" />
                                <div class="pm-help">域名键由地址自动推导，改地址会重建键；图标/统计/账号快照都按域名存。</div>
                            </div>
                        </div>

                        <div class="pm-grid">
                            <div class="pm-field span2">
                                <div class="pm-label">RSS 地址</div>
                                <el-input v-model="draft.rss_url" placeholder="https://pt.example.com/torrentrss.php?passkey=..." />
                                <div class="pm-help">订阅模式用的 RSS 链接，留空则用站点页面抓取。</div>
                            </div>
                        </div>

                        <div class="pm-grid">
                            <div class="pm-field">
                                <div class="pm-label">超时（秒）</div>
                                <el-input-number v-model="draft.timeout" :min="0" :max="300" controls-position="right" style="width:100%;" />
                                <div class="pm-help">0 表示不限制。</div>
                            </div>
                            <div class="pm-field">
                                <div class="pm-label">备注</div>
                                <el-input v-model="draft.note" placeholder="例如 主刷站 / 只刷免费种 / 每月上限 2 TiB" />
                            </div>
                        </div>

                        <div class="pm-grid">
                            <div class="pm-field">
                                <div class="pm-label">下载器</div>
                                <el-select v-model="draft.downloader" placeholder="选择下载器" style="width:100%;">
                                    <el-option
                                        v-for="d in downloaders"
                                        :key="d.alias"
                                        :value="d.alias"
                                        :label="d.alias + (d.url ? ' · ' + shortHost(d.url) : '')" />
                                </el-select>
                                <div class="pm-help">种子推给哪个 qBittorrent。选项来自「下载器配置」页的别名；没选过就默认用第一个。</div>
                            </div>
                            <div class="pm-field">
                                <div class="pm-label">保存路径</div>
                                <el-input v-model="draft.save_path" :placeholder="savePathPlaceholder" clearable />
                                <div class="pm-help">
                                    这个站点的种子落到哪个目录（qB 所在机器上的绝对路径）。
                                    留空就用所绑定下载器的默认保存路径，在「下载器配置」页里改。
                                </div>
                            </div>
                        </div>

                    </div>

                    <el-tabs v-model="apiTab" class="pm-tabs">
                        <el-tab-pane label="COOKIE" name="cookie">
                            <div class="pm-field">
                                <div class="pm-label">站点 Cookie</div>
                                <el-input
                                    v-model="draft.cookie"
                                    type="textarea"
                                    :rows="5"
                                    :placeholder="secretPlaceholder(draft.cookie, '粘贴浏览器里该站点的整条 Cookie')" />
                                <div class="pm-help">F12 → 网络 → 任意请求 → 请求头里的整条 Cookie；留空保存即沿用旧值。</div>
                            </div>
                        </el-tab-pane>
                        <el-tab-pane label="API" name="api">
                            <div class="pm-grid">
                                <div class="pm-field span2">
                                    <div class="pm-label">请求头 Authorization</div>
                                    <el-input
                                        :model-value="headerValue('Authorization')"
                                        placeholder="Bearer xxxxxxxx"
                                        @update:model-value="v => setHeaderValue('Authorization', v)" />
                                    <div class="pm-help">少数站点用 Authorization 头鉴权。</div>
                                </div>
                            </div>
                            <div class="pm-grid">
                                <div class="pm-field">
                                    <div class="pm-label">API Key</div>
                                    <el-input v-model="draft.api_key" :placeholder="secretPlaceholder(draft.api_key, '例如 M-Team 的 x-api-key')" />
                                </div>
                                <div class="pm-field">
                                    <div class="pm-label">备用令牌 Token</div>
                                    <el-input v-model="draft.token" :placeholder="secretPlaceholder(draft.token, '一般留空')" />
                                    <div class="pm-help">有 API Key 时优先用 API Key。M-Team 只填 API Key 只能读账号名，要读上传/下载量必须填 Cookie。</div>
                                </div>
                            </div>
                        </el-tab-pane>
                    </el-tabs>

                    <div class="pm-limit-head">
                        <el-switch v-model="limitPanel" />
                        <span class="pm-limit-head-text">限制站点访问频率</span>
                    </div>

                    <div v-if="limitPanel" class="pm-limit">
                        <div class="pm-limit-cards">
                            <div class="pm-limit-col">
                            <div class="pm-limit-card">
                                <span class="pm-limit-tag">单位周期（秒）</span>
                                <div class="pm-limit-body">
                                    <span class="pm-limit-ico">
                                        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
                                            <circle cx="12" cy="12" r="9" />
                                            <path d="M12 7.5v5l3 1.8" />
                                        </svg>
                                    </span>
                                    <el-input-number
                                        v-model="draft.limit_interval"
                                        :min="0"
                                        :max="86400"
                                        :controls="false"
                                        class="pm-limit-input" />
                                </div>
                            </div>
                                <div class="pm-limit-hint">限流控制的单位周期时长</div>
                            </div>
                            <div class="pm-limit-col">
                            <div class="pm-limit-card">
                                <span class="pm-limit-tag">周期内访问次数</span>
                                <div class="pm-limit-body">
                                    <span class="pm-limit-ico">
                                        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
                                            <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
                                            <path d="M7.5 9.5h5M7.5 13h9M7.5 16.5h6" />
                                        </svg>
                                    </span>
                                    <el-input-number
                                        v-model="draft.limit_count"
                                        :min="0"
                                        :max="1000"
                                        :controls="false"
                                        class="pm-limit-input" />
                                </div>
                            </div>
                                <div class="pm-limit-hint">单位周期内允许的访问次数</div>
                            </div>
                            <div class="pm-limit-col">
                            <div class="pm-limit-card">
                                <span class="pm-limit-tag">访问间隔（秒）</span>
                                <div class="pm-limit-body">
                                    <span class="pm-limit-ico">
                                        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
                                            <path d="M7 3.5h10M7 20.5h10M8 3.5c0 4 4 5.5 4 5.5s4-1.5 4-5.5M8 20.5c0-4 4-5.5 4-5.5s4 1.5 4 5.5" />
                                            <path d="M10.2 9.4h3.6v1.1h-3.6z" fill="currentColor" stroke="none" />
                                            <path d="M12 12v1.2" />
                                        </svg>
                                    </span>
                                    <el-input-number
                                        v-model="draft.limit_seconds"
                                        :min="0"
                                        :max="3600"
                                        :controls="false"
                                        class="pm-limit-input" />
                                </div>
                            </div>
                                <div class="pm-limit-hint">每次访问需要间隔的最小时间</div>
                            </div>
                        </div>
                        <div class="pm-limit-foot">
                            三项都填 0 表示不限制。抓种、下载种子、刷新账号数据共用这一份额度；站点回 429 时会自动整站冷却，不影响其它站点。
                        </div>
                    </div>

                    <div class="pm-section pm-section-tight">
                        <div class="pm-field">
                            <div class="pm-label">站点 User-Agent</div>
                            <el-input v-model="draft.user_agent" placeholder="留空则用默认 UA" />
                            <div class="pm-help">建议和拿 Cookie 的浏览器保持一致。</div>
                        </div>
                    </div>

                    <div class="pm-footer">
                        <el-button :loading="testing" @click="testDraft">测试此站点</el-button>
                        <div class="pm-footer-right">
                            <el-button @click="closeModal">取消</el-button>
                            <el-button type="primary" :loading="saving" @click="saveDraft">保存</el-button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `,
});
