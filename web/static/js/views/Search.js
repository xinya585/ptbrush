import { defineComponent, reactive, ref, computed, onMounted, nextTick } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';

// 搜片：关键字 → TMDB 候选海报 → 选中一部（跨站搜资源在下一步接入）
// 站点开关只影响搜片，和「刷流配置」里那套开关各管一套、互不干扰
export default defineComponent({
    name: 'Search',
    setup() {
        const sites = ref([]);
        const settings = ref({ has_key: false, tmdb_api_url: '', language: 'zh-CN', sites: {} });
        const loadingSites = ref(false);
        const keyword = ref('');
        const searching = ref(false);
        const items = ref([]);
        const searched = ref('');
        const selected = ref(null);
        const page = ref(1);
        const totalPages = ref(1);

        // 详情弹窗：点海报就弹，数据来自 TMDB 详情接口
        const showDetail = ref(false);
        // 弹窗宽度跟着主页面内容宽度走，和上面的卡片左右对齐
        const dialogWidth = ref('1040px');
        function syncDialogWidth() {
            const page = document.querySelector('.search-page');
            const w = page ? Math.round(page.getBoundingClientRect().width) : 0;
            if (w > 320) dialogWidth.value = w + 'px';
        }
        const detail = ref(null);
        const detailLoading = ref(false);

        // 设置面板（TMDB Key / 接口地址 / 语言）
        const showSettings = ref(false);
        const settingsForm = reactive({ tmdb_api_key: '', tmdb_api_url: '', language: 'zh-CN' });
        const savingSettings = ref(false);

        // 演员详情弹窗：点演职员头像进，含作品年表 + 作品内搜索
        const showPerson = ref(false);
        const person = ref(null);
        const personLoading = ref(false);
        const personKeyword = ref('');

        // 跨站搜资源：选中作品后去参与的 PT 站并发搜种子
        const showRes = ref(false);
        const resLoading = ref(false);
        const resData = ref(null);
        const resError = ref('');
        const resTarget = ref(null);

        // 种子列表的排序 / 筛选（只在本地过滤，不重新请求站点）
        const resSort = ref('seeders');
        const resFilter = reactive({ promo: [], res: [], medium: [], codec: [] });
        const RES_DIMS = ['promo', 'res', 'medium', 'codec'];

        function toggleResFilter(dim, val) {
            const arr = resFilter[dim];
            const at = arr.indexOf(val);
            if (at >= 0) arr.splice(at, 1);
            else arr.push(val);
        }

        function isResFilterOn(dim, val) {
            return resFilter[dim].includes(val);
        }

        function resetResFilters() {
            resSort.value = 'seeders';
            RES_DIMS.forEach(d => { resFilter[d] = []; });
        }

        function resFilterActive() {
            return RES_DIMS.some(d => resFilter[d].length > 0);
        }

        function resPass(r) {
            for (const d of RES_DIMS) {
                if (resFilter[d].length && !resFilter[d].includes(r[d])) return false;
            }
            return true;
        }

        // 分站分组渲染：先按筛选裁剪，再按当前排序重排；count 换成筛后条数，raw 保留原始条数
        const resView = computed(() => {
            const data = resData.value;
            if (!data) return { sites: [], total: 0 };
            const sites = (data.sites || []).map((s) => {
                let rows = (s.results || []).filter(resPass);
                if (resSort.value === 'size_desc') rows = rows.slice().sort((a, b) => Number(b.size) - Number(a.size));
                else if (resSort.value === 'size_asc') rows = rows.slice().sort((a, b) => Number(a.size) - Number(b.size));
                return { ...s, results: rows, count: rows.length, rawCount: s.count };
            });
            return { sites, total: sites.reduce((n, s) => n + s.results.length, 0) };
        });

        function promoLabel(p) {
            const map = { free: '免费', free_2x: '免费 2X', '2x': '2X', half_down: '半价', none: '' };
            return map[p] || '';
        }

        function promoClass(p) {
            if (p === 'free' || p === 'free_2x') return 'rs-promo rs-promo-free';
            if (p === '2x') return 'rs-promo rs-promo-2x';
            if (p === 'half_down') return 'rs-promo rs-promo-half';
            return 'rs-promo';
        }

        async function openResources(item) {
            const target = item || selected.value;
            if (!target) return;
            if (!onCount.value) {
                ElMessage.warning('先在上面打开至少一个参与搜片的站点');
                return;
            }
            syncDialogWidth();
            // 同样只留一个内窗：开搜资源前把详情/演员窗收起来
            showDetail.value = false;
            showPerson.value = false;
            showRes.value = true;
            resLoading.value = true;
            resData.value = null;
            resError.value = '';
            resTarget.value = target;
            resetResFilters();
            try {
                const q = new URLSearchParams({
                    title: String(target.title || ''),
                    original_title: String(target.original_title || ''),
                    type: String(target.media_type || ''),
                    id: String(target.tmdb_id || ''),
                });
                resData.value = await api.get('/api/search/resources?' + q.toString());
            } catch (e) {
                const msg = (e && e.message) || '';
                resError.value = /Failed to fetch|NetworkError|Load failed|timeout/i.test(msg)
                    ? '请求被中断：可能是某个站点太慢把连接拖断了，或者后端正在重启。点重试即可再来一次。'
                    : (msg || '搜资源失败');
            } finally {
                resLoading.value = false;
            }
        }

        function downloadTorrent(item) {
            if (!item) return;
            const q = new URLSearchParams({
                site: String(item.site_key || ''),
                torrent_id: String(item.id || ''),
                name: String(item.name || ''),
            });
            const a = document.createElement('a');
            a.href = '/api/search/dl?' + q.toString();
            a.rel = 'noopener';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        }

        // 从搜资源退回上一层的作品详情（搜资源是由详情/已选条进来的）
        function backFromRes() {
            const target = resTarget.value;
            showRes.value = false;
            if (target && target.tmdb_id) {
                openDetail(target);
            }
        }

        function retryRes() {
            if (resTarget.value) openResources(resTarget.value);
        }

        const personWorks = computed(() => {
            const list = (person.value && person.value.works) || [];
            const kw = String(personKeyword.value || '').trim().toLowerCase();
            if (!kw) return list;
            return list.filter(w => (
                String(w.title || '').toLowerCase().includes(kw)
                || String(w.original_title || '').toLowerCase().includes(kw)
                || String(w.character || '').toLowerCase().includes(kw)
                || String(w.job || '').toLowerCase().includes(kw)
                || String(w.year || '') === kw
            ));
        });

        const siteList = computed(() => sites.value.slice().sort(
            (a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN')
        ));

        function flagOf(site) {
            const key = String(site.key || site.domain || '').toLowerCase();
            const flags = settings.value.sites || {};
            // 没记录 = 参与（和刷流「名单留空=全部参与」同一个直觉）
            return flags[key] !== false;
        }

        const onCount = computed(() => siteList.value.filter(s => flagOf(s)).length);

        async function loadSettings() {
            try {
                const data = await api.get('/api/search/settings');
                settings.value = data;
                settingsForm.tmdb_api_url = data.tmdb_api_url || '';
                settingsForm.language = data.language || 'zh-CN';
                settingsForm.tmdb_api_key = '';
            } catch (e) {
                ElMessage.error(e.message || '读取搜片设置失败');
            }
        }

        async function loadSites() {
            loadingSites.value = true;
            try {
                // 用搜片专用接口：/api/sites 要聚合 qB 做种数，没配下载器时会等十几秒
                const data = await api.get('/api/search/sites');
                sites.value = data.sites || [];
                if (data.flags) {
                    settings.value = { ...settings.value, sites: data.flags };
                }
            } catch (e) {
                ElMessage.error(e.message || '读取站点失败');
            } finally {
                loadingSites.value = false;
            }
        }

        // 开关立即保存，不用再点「保存」：和刷流页的手感保持一致
        async function toggleSite(site, on) {
            const key = String(site.key || site.domain || '').toLowerCase();
            const next = { ...(settings.value.sites || {}) };
            next[key] = !!on;
            settings.value = { ...settings.value, sites: next };
            try {
                await api.post('/api/search/sites', { domain: key, enabled: !!on });
            } catch (e) {
                // 存失败就回滚，避免界面显示的和磁盘上的不一致
                const rolled = { ...next };
                delete rolled[key];
                settings.value = { ...settings.value, sites: rolled };
                ElMessage.error(e.message || '保存站点开关失败');
            }
        }

        async function setAll(on) {
            const targets = siteList.value;
            if (!targets.length) return;
            const next = { ...(settings.value.sites || {}) };
            for (const site of targets) {
                const key = String(site.key || site.domain || '').toLowerCase();
                next[key] = !!on;
            }
            settings.value = { ...settings.value, sites: next };
            try {
                await Promise.all(targets.map(site => api.post('/api/search/sites', {
                    domain: String(site.key || site.domain || '').toLowerCase(),
                    enabled: !!on,
                })));
                ElMessage.success(on ? '已全部打开' : '已全部关闭');
            } catch (e) {
                ElMessage.error(e.message || '批量保存失败');
                await loadSettings();
            }
        }

        function posterUrl(item, size = 'w300') {
            if (!item || !item.poster) return '';
            return `/api/search/poster?size=${size}&path=${encodeURIComponent(item.poster)}`;
        }

        function backdropUrl(item) {
            if (!item || !item.backdrop) return '';
            return `/api/search/poster?size=w780&path=${encodeURIComponent(item.backdrop)}`;
        }

        function ytThumb(key) {
            return `/api/search/ytthumb?key=${encodeURIComponent(key)}`;
        }

        // 演员头像也走同一条图片代理，墙内/无直连都能出图
        function personUrl(profile) {
            if (!profile) return '';
            return `/api/search/poster?size=w185&path=${encodeURIComponent(profile)}`;
        }

        // 预告片被下架/设私有时缩略图拉不到，隐藏图片露出底下的播放图标占位
        function hideThumb(event) {
            if (event && event.target) event.target.classList.add('is-broken');
        }

        function typeLabel(item) {
            return item.media_type === 'tv' ? '剧集' : '电影';
        }

        async function doSearch(nextPage = 1) {
            const kw = String(keyword.value || '').trim();
            if (!kw) {
                ElMessage.warning('先输一个片名');
                return;
            }
            if (!settings.value.has_key) {
                showSettings.value = true;
                ElMessage.warning('先填 TMDB API Key 才能搜');
                return;
            }
            searching.value = true;
            try {
                const data = await api.get(`/api/search/tmdb?keyword=${encodeURIComponent(kw)}&page=${nextPage}`);
                items.value = data.items || [];
                searched.value = data.keyword || kw;
                page.value = data.page || nextPage;
                totalPages.value = data.total_pages || 1;
                selected.value = null;
                if (!items.value.length) ElMessage.info('TMDB 没有找到匹配的作品，换个名字或试试英文名');
            } catch (e) {
                ElMessage.error(e.message || '搜索失败');
            } finally {
                searching.value = false;
            }
        }

        async function loadTrending() {
            if (!settings.value.has_key) return;
            try {
                const data = await api.get('/api/search/tmdb?kind=trending');
                if (!items.value.length) {
                    items.value = data.items || [];
                    searched.value = '';
                }
            } catch (e) {
                // 热门只是占位内容，失败了不用打扰用户
            }
        }

        function select(item) { selected.value = item; }

        async function openDetail(item) {
            if (!item) return;
            syncDialogWidth();
            showDetail.value = true;
            detail.value = null;
            detailLoading.value = true;
            try {
                const data = await api.get(
                    `/api/search/detail?type=${encodeURIComponent(item.media_type)}&id=${encodeURIComponent(item.tmdb_id)}`
                );
                detail.value = data;
            } catch (e) {
                ElMessage.error(e.message || '取详情失败');
            } finally {
                detailLoading.value = false;
            }
        }

        // 点海报：直接弹媒体详情（详情里再点「用这 N 个站搜资源」）
        function openItem(item) {
            selected.value = item;
            openDetail(item);
        }

        // 点演职员头像 → 演员详情（作品年表 + 作品内搜索）
        async function openPerson(p) {
            if (!p || !p.id) return;
            // 只保留一个内窗：开演员详情时把媒体详情关掉
            showDetail.value = false;
            syncDialogWidth();
            showPerson.value = true;
            person.value = null;
            personKeyword.value = '';
            personLoading.value = true;
            try {
                const data = await api.get(`/api/search/person?id=${encodeURIComponent(p.id)}`);
                person.value = data;
            } catch (e) {
                ElMessage.error(e.message || '取演员详情失败');
            } finally {
                personLoading.value = false;
            }
        }

        // 演员作品里点一部 → 直接打开那部的媒体详情
        function openWork(work) {
            showPerson.value = false;
            openItem({
                tmdb_id: work.tmdb_id,
                media_type: work.media_type,
                title: work.title,
                original_title: work.original_title,
                year: work.year,
                poster: work.poster,
                vote: work.vote,
            });
        }

        // 演员大图走图片代理，尺寸单列
        function personPhoto(profile) {
            if (!profile) return '';
            return `/api/search/poster?size=w300&path=${encodeURIComponent(profile)}`;
        }

        async function saveSettings() {
            savingSettings.value = true;
            try {
                const payload = {
                    tmdb_api_url: settingsForm.tmdb_api_url,
                    language: settingsForm.language,
                };
                if (String(settingsForm.tmdb_api_key || '').trim()) {
                    payload.tmdb_api_key = String(settingsForm.tmdb_api_key).trim();
                }
                const data = await api.put('/api/search/settings', payload);
                settings.value = data;
                settingsForm.tmdb_api_key = '';
                showSettings.value = false;
                ElMessage.success('已保存');
                if (String(keyword.value || '').trim()) doSearch(1);
                else loadTrending();
            } catch (e) {
                ElMessage.error(e.message || '保存失败');
            } finally {
                savingSettings.value = false;
            }
        }

        onMounted(async () => {
            await Promise.all([loadSettings(), loadSites()]);
            loadTrending();
        });

        return {
            sites, siteList, settings, loadingSites, keyword, searching, items, searched, selected,
            page, totalPages, showSettings, settingsForm, savingSettings, onCount,
            showDetail, detail, detailLoading, dialogWidth,
            showPerson, person, personLoading, personKeyword, personWorks, openPerson, openWork, personPhoto,
            showRes, resLoading, resData, resError, resTarget, openResources, downloadTorrent, promoLabel, promoClass,
            resView, resSort, resFilter, toggleResFilter, isResFilterOn, resetResFilters, resFilterActive,
            backFromRes, retryRes,
            flagOf, toggleSite, setAll, posterUrl, backdropUrl, ytThumb, personUrl, hideThumb, typeLabel, doSearch,
            select, openItem, openDetail, saveSettings, loadSettings, loadSites,
        };
    },
    template: `
        <div class="config-page search-page">
            <div class="config-page-header">
                <div>
                    <h2>搜片</h2>
                    <div class="config-page-sub">
                        输片名先在 TMDB 认片，出海报候选；选中的片再用下面这套站点名单去搜资源。
                    </div>
                </div>
                <div class="config-actions">
                    <el-button plain @click="showSettings = !showSettings">
                        {{ settings.has_key ? 'TMDB 设置' : '填 TMDB Key' }}
                    </el-button>
                    <el-button :loading="loadingSites" plain @click="loadSites">刷新站点</el-button>
                </div>
            </div>

            <el-card v-if="showSettings" style="margin-bottom:16px;" shadow="hover">
                <template #header><strong>TMDB 设置</strong></template>
                <el-form label-position="top">
                    <div class="form-grid">
                        <el-form-item class="form-full" label="TMDB API Key（v3）">
                            <el-input
                                v-model="settingsForm.tmdb_api_key"
                                clearable
                                :placeholder="settings.has_key ? '已配置，留空表示不改' : '粘贴你的 TMDB API Key'" />
                            <div class="field-help">
                                在 themoviedb.org 注册后「设置 → API」里能找到（v3 auth 那串）。已经配过就留空，不会覆盖。
                            </div>
                        </el-form-item>
                        <el-form-item label="TMDB 接口地址">
                            <el-input v-model="settingsForm.tmdb_api_url" clearable placeholder="https://api.themoviedb.org/3" />
                            <div class="field-help">国内直连不稳时，可以换成自建或镜像的反代地址。</div>
                        </el-form-item>
                        <el-form-item label="语言">
                            <el-input v-model="settingsForm.language" clearable placeholder="zh-CN" />
                            <div class="field-help">zh-CN 出中文标题和简介；英文名搜不到时可以换成 en-US 试。</div>
                        </el-form-item>
                    </div>
                </el-form>
                <div style="text-align:right;">
                    <el-button type="primary" :loading="savingSettings" @click="saveSettings">保存</el-button>
                </div>
            </el-card>

            <el-card style="margin-bottom:16px;" shadow="hover">
                <template #header>
                    <div class="card-header-row">
                        <div>
                            <strong>参与搜片的站点</strong>
                            <span class="search-count">已启用 {{ onCount }} / {{ siteList.length }}</span>
                        </div>
                        <div class="config-actions">
                            <el-button size="small" plain @click="setAll(true)">全开</el-button>
                            <el-button size="small" plain @click="setAll(false)">全关</el-button>
                        </div>
                    </div>
                </template>
                <div class="site-toggle-list" style="width:100%;">
                    <div v-for="s in siteList" :key="s.key || s.domain" class="site-toggle-row">
                        <div class="site-toggle-main">
                            <strong class="site-toggle-name">{{ s.name }}</strong>
                            <span class="site-toggle-domain">{{ s.key || s.domain }}</span>
                            <el-tag v-if="s.type === 'mteam'" size="small" type="warning">API</el-tag>
                        </div>
                        <el-switch :model-value="flagOf(s)" @change="toggleSite(s, $event)" />
                    </div>
                    <div v-if="loadingSites" class="site-toggle-empty">正在读取站点…</div>
                    <div v-else-if="!siteList.length" class="site-toggle-empty">
                        还没有配置站点，先去「站点配置」页添加。
                    </div>
                </div>
                <div class="field-help">
                    这套开关只决定「搜片时去哪些站搜」，关掉的站不参与搜片，也不影响刷流。改动立即保存。
                </div>
            </el-card>

            <el-card style="margin-bottom:16px;" shadow="hover">
                <template #header>
                    <div class="card-header-row">
                        <strong>搜索作品</strong>
                        <span class="search-count" v-if="searched">「{{ searched }}」共 {{ items.length }} 个候选</span>
                    </div>
                </template>

                <div class="search-bar">
                    <el-input
                        v-model="keyword"
                        size="large"
                        clearable
                        placeholder="输片名，中文英文都行，例如 复仇者联盟 / The Avengers"
                        @keyup.enter="doSearch(1)">
                        <template #append>
                            <el-button :loading="searching" @click="doSearch(1)">搜索</el-button>
                        </template>
                    </el-input>
                </div>

                <div v-if="!settings.has_key" class="search-empty">
                    还没配 TMDB API Key，点右上角「填 TMDB Key」填一个就能搜了。
                </div>
                <div v-else-if="!items.length && !searching" class="search-empty">
                    {{ searched ? '没有候选，换个片名再试' : '上面是本周热门，输片名开始搜' }}
                </div>

                <div v-if="items.length" class="poster-grid">
                    <div
                        v-for="item in items"
                        :key="item.tmdb_id + '-' + item.media_type"
                        class="poster-card"
                        :class="{ active: selected && selected.tmdb_id === item.tmdb_id && selected.media_type === item.media_type }"
                        @click="openItem(item)">
                        <div class="poster-thumb">
                            <img v-if="item.poster" :src="posterUrl(item)" loading="lazy" alt="" />
                            <div v-else class="poster-none">无海报</div>
                            <div class="poster-score" v-if="item.vote">{{ item.vote }}</div>
                        </div>
                        <div class="poster-meta">
                            <div class="poster-title" :title="item.title">{{ item.title }}</div>
                            <div class="poster-sub">
                                <span>{{ item.year || '—' }}</span>
                                <span class="poster-type">{{ typeLabel(item) }}</span>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="search-pager" v-if="totalPages > 1 && searched">
                    <el-pagination
                        layout="prev, pager, next"
                        :current-page="page"
                        :page-size="20"
                        :total="totalPages * 20"
                        @current-change="doSearch" />
                </div>
            </el-card>

            <el-dialog
                v-model="showDetail"
                :width="dialogWidth"
                top="6vh"
                class="detail-dialog"
                destroy-on-close>
                <template #header>
                    <div class="detail-dialog-title">
                        媒体详情<span v-if="detail">-{{ detail.title }}<span v-if="detail.year">（{{ detail.year }}）</span></span>
                    </div>
                </template>

                <div v-if="detailLoading" class="detail-loading">
                    <el-skeleton :rows="6" animated />
                </div>

                <div v-else-if="detail" class="media-detail">
                    <div class="md-hero" :style="detail.backdrop ? { backgroundImage: 'url(' + backdropUrl(detail) + ')' } : {}">
                        <div class="md-hero-mask"></div>
                        <div class="md-hero-body">
                            <div class="md-poster">
                                <img v-if="detail.poster" :src="posterUrl(detail, 'w342')" alt="" />
                                <div v-else class="poster-none">无海报</div>
                            </div>
                            <div class="md-head">
                                <div class="md-title">
                                    {{ detail.title }}<span class="md-year" v-if="detail.year">（{{ detail.year }}）</span>
                                </div>
                                <div class="md-original" v-if="detail.original_title && detail.original_title !== detail.title">
                                    {{ detail.original_title }}
                                </div>
                                <div class="md-meta">
                                    <span v-if="detail.vote" class="md-score">{{ detail.vote }}</span>
                                    <span v-if="detail.vote_count" class="md-votes">{{ detail.vote_count }} 人评分</span>
                                    <span class="md-sep" v-if="detail.runtime_text">·</span>
                                    <span v-if="detail.runtime_text">{{ detail.runtime_text }}</span>
                                    <span class="md-sep" v-if="detail.genres.length">·</span>
                                    <span>{{ detail.genres.join(' / ') }}</span>
                                    <span class="md-sep" v-if="detail.media_type === 'tv' && detail.seasons">·</span>
                                    <span v-if="detail.media_type === 'tv' && detail.seasons">共 {{ detail.seasons }} 季 / {{ detail.episodes }} 集</span>
                                </div>
                                <div class="md-ids">
                                    <span>TMDB ID: <b>{{ detail.tmdb_id }}</b></span>
                                    <span v-if="detail.imdb_id">IMDB ID: <b>{{ detail.imdb_id }}</b></span>
                                </div>
                                <div class="md-actions">
                                    <el-button type="primary" :disabled="!onCount" @click="openResources(detail)">
                                        用这 {{ onCount }} 个站搜资源
                                    </el-button>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="md-panel">
                        <div class="md-panel-main">
                            <div class="md-section-title">简介</div>
                            <div class="md-overview">{{ detail.overview || 'TMDB 上还没有中文简介。' }}</div>
                        </div>
                        <div class="md-aside">
                            <div class="md-stat"><span>状态</span><b>{{ detail.status || '—' }}</b></div>
                            <div class="md-stat"><span>上映日期</span><b>{{ detail.release_date || '—' }}</b></div>
                            <div class="md-stat" v-if="detail.runtime_text"><span>片长</span><b>{{ detail.runtime_text }}</b></div>
                            <div class="md-stat"><span>评分</span><b>{{ detail.vote || '—' }}</b></div>
                            <div class="md-stat" v-if="detail.countries.length"><span>地区</span><b>{{ detail.countries.join('、') }}</b></div>
                            <div class="md-stat" v-if="detail.languages.length"><span>语言</span><b>{{ detail.languages.join('、') }}</b></div>
                        </div>
                    </div>

                    <div class="md-section" v-if="detail.directors.length || detail.cast.length">
                        <div class="md-section-title">演职员</div>
                        <template v-if="detail.directors.length">
                            <div class="md-cast-label">导演 / 主创</div>
                            <div class="md-cast-row">
                                <div class="md-cast-card" v-for="(d, i) in detail.directors" :key="'d' + i" @click="openPerson(d)">
                                    <div class="md-cast-photo">
                                        <span class="md-cast-initial">{{ (d.name || '?').charAt(0) }}</span>
                                        <img v-if="d.profile" :src="personUrl(d.profile)" alt="" loading="lazy" @error="hideThumb($event)" />
                                    </div>
                                    <div class="md-cast-name" :title="d.name">{{ d.name }}</div>
                                    <div class="md-cast-role">{{ d.job }}</div>
                                </div>
                            </div>
                        </template>
                        <template v-if="detail.cast.length">
                            <div class="md-cast-label">演员阵容</div>
                            <div class="md-cast-row">
                                <div class="md-cast-card" v-for="(c, i) in detail.cast" :key="'c' + i" @click="openPerson(c)">
                                    <div class="md-cast-photo">
                                        <span class="md-cast-initial">{{ (c.name || '?').charAt(0) }}</span>
                                        <img v-if="c.profile" :src="personUrl(c.profile)" alt="" loading="lazy" @error="hideThumb($event)" />
                                    </div>
                                    <div class="md-cast-name" :title="c.name">{{ c.name }}</div>
                                    <div class="md-cast-role" :title="c.character">{{ c.character }}</div>
                                </div>
                            </div>
                        </template>
                    </div>

                    <div class="md-section" v-if="detail.videos.length">
                        <div class="md-section-title">预告片</div>
                        <div class="md-trailers">
                            <a
                                v-for="v in detail.videos"
                                :key="v.key"
                                class="md-trailer"
                                :href="'https://www.youtube.com/watch?v=' + v.key"
                                target="_blank"
                                rel="noopener">
                                <span class="md-trailer-thumb">
                                    <img :src="ytThumb(v.key)" alt="" loading="lazy" @error="hideThumb($event)" />
                                </span>
                                <span class="md-trailer-name">{{ v.name }}</span>
                            </a>
                        </div>
                    </div>
                </div>

                <div v-else class="detail-loading">详情没取到，关掉重开一次试试。</div>
            </el-dialog>

            <el-dialog
                v-model="showPerson"
                :width="dialogWidth"
                top="6vh"
                class="detail-dialog person-dialog"
                destroy-on-close>
                <template #header>
                    <div class="detail-dialog-title">
                        演员详情<span v-if="person">-{{ person.name }}</span>
                    </div>
                </template>

                <div v-if="personLoading" class="detail-loading">
                    <el-skeleton :rows="6" animated />
                </div>

                <div v-else-if="person" class="person-body">
                    <div class="pd-head">
                        <div class="pd-photo">
                            <span class="md-cast-initial">{{ (person.name || '?').charAt(0) }}</span>
                            <img v-if="person.profile" :src="personPhoto(person.profile)" alt="" @error="hideThumb($event)" />
                        </div>
                        <div class="pd-meta">
                            <div class="pd-name">{{ person.name }}</div>
                            <div class="pd-tags">
                                <span class="pd-tag" v-if="person.department">{{ person.department }}</span>
                                <span class="pd-tag" v-if="person.birthday">生于 {{ person.birthday }}</span>
                                <span class="pd-tag" v-if="person.deathday">卒于 {{ person.deathday }}</span>
                                <span class="pd-tag" v-if="person.place_of_birth">{{ person.place_of_birth }}</span>
                                <span class="pd-tag" v-if="person.imdb_id">IMDB {{ person.imdb_id }}</span>
                            </div>
                            <div class="pd-bio" v-if="person.biography">{{ person.biography }}</div>
                            <div class="pd-bio pd-bio-empty" v-else>TMDB 上还没有这位的中文简介。</div>
                        </div>
                    </div>

                    <div class="pd-works-head">
                        <div class="md-section-title">推荐影片</div>
                        <div class="pd-works-search">
                            <el-input
                                v-model="personKeyword"
                                size="small"
                                clearable
                                placeholder="在这位演员的作品里搜片名 / 角色 / 年份" />
                        </div>
                    </div>
                    <div class="pd-works-count">
                        共 {{ personWorks.length }} 部{{ personKeyword ? '（已按关键字过滤）' : '' }}
                    </div>

                    <div class="pd-works" v-if="personWorks.length">
                        <div
                            v-for="w in personWorks"
                            :key="w.media_type + '-' + w.tmdb_id"
                            class="pd-work"
                            @click="openWork(w)">
                            <div class="pd-work-thumb">
                                <img v-if="w.poster" :src="posterUrl(w, 'w185')" loading="lazy" alt="" />
                                <div v-else class="poster-none">无海报</div>
                                <div class="poster-score" v-if="w.vote">{{ w.vote }}</div>
                            </div>
                            <div class="pd-work-title" :title="w.title">{{ w.title }}</div>
                            <div class="pd-work-sub" :title="w.character || w.job">
                                {{ w.year || '—' }} · {{ w.media_type === 'tv' ? '剧集' : '电影' }}
                            </div>
                        </div>
                    </div>
                    <div v-else class="search-empty">按这个关键字没搜到作品，清空再试。</div>
                </div>

                <div v-else class="detail-loading">演员详情没取到，关掉重开一次试试。</div>
            </el-dialog>

            <el-dialog
                v-model="showRes"
                :width="dialogWidth"
                top="6vh"
                class="detail-dialog res-dialog"
                destroy-on-close>
                <template #header>
                    <div class="detail-dialog-title res-head">
                        <button type="button" class="res-back" @click="backFromRes">‹ 返回</button>
                        <span class="res-head-title">搜资源<span v-if="resTarget">-{{ resTarget.title }}</span></span>
                    </div>
                </template>

                <div v-if="resLoading" class="detail-loading">
                    <el-skeleton :rows="8" animated />
                    <div class="res-hint">正在参与的站点里并发搜索，站点多时十几秒才回来。</div>
                </div>

                <div v-else-if="resError" class="res-error">
                    <div class="search-empty">{{ resError }}</div>
                    <el-button size="small" plain @click="retryRes">重试</el-button>
                </div>

                <div v-else-if="resData" class="res-body">
                    <div class="res-sum">
                        <span class="res-kw" v-for="kw in resData.keywords" :key="kw">{{ kw }}</span>
                        <span class="res-total">
                            {{ resData.total }} 条资源 ·
                            {{ resData.sites.filter(s => s.count).length }} / {{ resData.sites.length }} 个站有结果
                            <span v-if="resFilterActive()" class="res-filtered">筛选后 {{ resView.total }} 条</span>
                        </span>
                    </div>

                    <div class="res-filters">
                        <div class="rf-row">
                            <span class="rf-label">排序</span>
                            <button type="button" class="rf-chip" :class="{ on: resSort === 'seeders' }"
                                    @click="resSort = 'seeders'">做种最多</button>
                            <button type="button" class="rf-chip" :class="{ on: resSort === 'size_desc' }"
                                    @click="resSort = 'size_desc'">体积大→小</button>
                            <button type="button" class="rf-chip" :class="{ on: resSort === 'size_asc' }"
                                    @click="resSort = 'size_asc'">体积小→大</button>
                            <button type="button" class="rf-reset" v-if="resFilterActive() || resSort !== 'seeders'"
                                    @click="resetResFilters">清空</button>
                        </div>
                        <div class="rf-row">
                            <span class="rf-label">促销</span>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('promo', 'free') }"
                                    @click="toggleResFilter('promo', 'free')">免费</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('promo', 'half') }"
                                    @click="toggleResFilter('promo', 'half')">半价</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('promo', 'other') }"
                                    @click="toggleResFilter('promo', 'other')">其他</button>
                        </div>
                        <div class="rf-row">
                            <span class="rf-label">分辨率</span>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('res', '2160') }"
                                    @click="toggleResFilter('res', '2160')">2160</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('res', '1440') }"
                                    @click="toggleResFilter('res', '1440')">1440</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('res', '1080') }"
                                    @click="toggleResFilter('res', '1080')">1080</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('res', '720') }"
                                    @click="toggleResFilter('res', '720')">720</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('res', 'other') }"
                                    @click="toggleResFilter('res', 'other')">其他</button>
                        </div>
                        <div class="rf-row">
                            <span class="rf-label">介质</span>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('medium', 'bluray') }"
                                    @click="toggleResFilter('medium', 'bluray')">BluRay</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('medium', 'remux') }"
                                    @click="toggleResFilter('medium', 'remux')">Remux</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('medium', 'webdl') }"
                                    @click="toggleResFilter('medium', 'webdl')">WEB-DL</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('medium', 'other') }"
                                    @click="toggleResFilter('medium', 'other')">其他</button>
                        </div>
                        <div class="rf-row">
                            <span class="rf-label">编码</span>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('codec', '264') }"
                                    @click="toggleResFilter('codec', '264')">H.264</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('codec', '265') }"
                                    @click="toggleResFilter('codec', '265')">H.265</button>
                            <button type="button" class="rf-chip" :class="{ on: isResFilterOn('codec', 'other') }"
                                    @click="toggleResFilter('codec', 'other')">其他</button>
                        </div>
                    </div>

                    <div v-if="!resData.total" class="search-empty">
                        这些站都没搜到符合条件的种子。可以少开几个站再试，或到站点里确认 cookie / api_key 是否有效。
                    </div>
                    <div v-else-if="!resView.total" class="search-empty">
                        当前筛选条件下没有种子，清空筛选再看。
                    </div>
                    <div v-else class="res-tip">
                        促销和 H&amp;R 取自站点列表页；个别站只在详情页标 H&amp;R，下载前可点标题到站点核对。
                    </div>

                    <div
                        v-for="s in resView.sites"
                        :key="s.key"
                        v-show="s.count || s.error"
                        class="rs-site">
                        <div class="rs-site-head">
                            <strong class="rs-site-name">{{ s.name }}</strong>
                            <span class="rs-site-count">
                                {{ s.count }} 条<span v-if="resFilterActive() && s.rawCount !== s.count"> / {{ s.rawCount }}</span>
                            </span>
                            <span v-if="s.error" class="rs-site-err">{{ s.error }}</span>
                        </div>
                        <div class="rs-rows">
                            <div v-for="r in s.results" :key="s.key + '-' + r.id" class="rs-row">
                                <div class="rs-title">
                                    <a class="rs-name" :href="r.detail_url" target="_blank" rel="noopener"
                                       :title="r.subtitle ? r.name + ' ｜ 副标题：' + r.subtitle : r.name">{{ r.name }}</a>
                                    <span v-if="promoLabel(r.promotion)" :class="promoClass(r.promotion)">{{ promoLabel(r.promotion) }}</span>
                                    <span v-if="r.hr" class="rs-hr" title="H&R 种子">H&R</span>
                                    <span v-if="r.zh_sub" class="rs-sub rs-sub-zh" title="主标题或副标题里标注了中文字幕">中字</span>
                                    <span v-if="r.sfx_sub" class="rs-sub rs-sub-sfx" title="主标题或副标题里标注了特效字幕">特效字幕</span>
                                </div>
                                <span class="rs-size">{{ r.size_text }}</span>
                                <span class="rs-seeders" :title="'做种 ' + r.seeders + ' / 下载 ' + r.leechers">
                                    做种 {{ r.seeders }}
                                </span>
                                <span class="rs-created">{{ r.created }}</span>
                                <el-button size="small" type="primary" plain @click="downloadTorrent(r)">下载种子</el-button>
                            </div>
                        </div>
                    </div>
                </div>

                <div v-else class="detail-loading">没拿到结果，关掉重开一次试试。</div>
            </el-dialog>
        </div>
    `,
});
