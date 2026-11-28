import { defineComponent, reactive, ref, computed, onMounted } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';
import SizeInput from '../components/SizeInput.js';
import WorkTimePicker from '../components/WorkTimePicker.js';

function emptyForm() {
    return {
        brush: {
            // 表单初始值；真正生效的值以 /api/config 返回为准
            min_disk_space: { value: 100, unit: 'GiB' },
            torrent_max_size: { value: 0, unit: 'GiB' },
            max_downloading_torrents: 5,
            download_speed_min: 2,
            slow_delete_minutes: 15,
            slow_delete_speed: 1,
            // 低速上传持续就删（盒子专用）：独立开关，默认关闭；阈值 0.1 MiB/s
            slow_up_delete_enabled: false,
            slow_up_delete_minutes: 0,
            slow_up_delete_speed: 0.1,
            max_no_activate_time: 0,
            free_end_delete_minutes: 20,
            work_time: '',
            work_time_hours: [],
            upload_cycle: 600,
            download_cycle: 1200,
            promotions: ['free', 'free_2x'],
            exclude_hr: true,
            min_free_hours: 5,
            brush_interval: 10,
            target_sites: [],
            // 名单留空时是否表示「一个都不参与」：只在关掉最后一个站点时才置 true，
            // 否则留空在旧语义里等于「全部参与」，唯一的开关会关掉又弹回来
            target_none: false,
            // 对齐 MoviePilot 刷流：分享率控制、删种条件、选种增强、检查周期
            site_ratio_control: false,
            site_ratio_target: 2,
            seed_time: 12,
            hr_seed_time: 72,
            seed_ratio: 1,
            seed_size: 0,
            download_time: 0,
            seed_avgspeed: 0,
            delete_size_range: '',
            include: '',
            exclude: '',
            size: '1-100',
            seeder: '20',
            pubtime: '60',
            fail_backoff_minutes: 1440,
            qb_category: 'ptbrush',
            check_interval: 5,
        },
        sites: [],
    };
}

// 「参与刷流的站点」用域名当键：站点名可以随便改（改成中文也不影响选择）
function siteKey(site) {
    // 后端 /api/config 已经算好稳定键（显式 domain 优先），优先用它，
    // 避免这里从 base_url 推导出 zp.m-team.io 这种和配置对不上的值
    if (site && site.key) return String(site.key).trim().toLowerCase();
    if (site && site.domain) return String(site.domain).trim().toLowerCase();
    const url = (site && (site.base_url || site.rss_url)) || '';
    let host = '';
    try {
        host = new URL(url).hostname.toLowerCase();
    } catch (e) {
        host = '';
    }
    host = host.replace(/^www\./, '');
    return host || (site && site.name) || '';
}

function stripPayload(form) {
    return {
        brush: {
            min_disk_space: { value: form.brush.min_disk_space.value, unit: form.brush.min_disk_space.unit },
            // 大包瘦身已下线：固定发 0，后端把 0 当作「不瘦身」，
            // 留着这个键只是为了防止后端 Pydantic 默认值（10GiB）把瘦身重新打开
            torrent_max_size: { value: 0, unit: 'GiB' },
            max_downloading_torrents: form.brush.max_downloading_torrents,
            download_speed_min: Number(form.brush.download_speed_min) || 0,
            slow_delete_minutes: Number(form.brush.slow_delete_minutes) || 0,
            slow_delete_speed: Number(form.brush.slow_delete_speed) || 0,
            slow_up_delete_enabled: !!form.brush.slow_up_delete_enabled,
            slow_up_delete_minutes: Number(form.brush.slow_up_delete_minutes) || 0,
            slow_up_delete_speed: Number(form.brush.slow_up_delete_speed) || 0,
            max_no_activate_time: form.brush.max_no_activate_time,
            free_end_delete_minutes: Number(form.brush.free_end_delete_minutes) || 0,
            work_time: form.brush.work_time || '',
            upload_cycle: form.brush.upload_cycle,
            download_cycle: form.brush.download_cycle,
            promotions: [...(form.brush.promotions || [])],
            exclude_hr: !!form.brush.exclude_hr,
            min_free_hours: form.brush.min_free_hours,
            brush_interval: form.brush.brush_interval,
            // 只保留当前还存在的站点（按域名键比对），站点删掉后不留下无效选择
            target_sites: (form.brush.target_sites || [])
                .filter(k => form.sites.some(s => siteKey(s) === k)),
            target_none: !!form.brush.target_none,
            // ---- 对齐 MoviePilot 刷流的新增项 ----
            site_ratio_control: !!form.brush.site_ratio_control,
            site_ratio_target: Number(form.brush.site_ratio_target) || 0,
            seed_time: Number(form.brush.seed_time) || 0,
            hr_seed_time: Number(form.brush.hr_seed_time) || 0,
            seed_ratio: Number(form.brush.seed_ratio) || 0,
            seed_size: Number(form.brush.seed_size) || 0,
            download_time: Number(form.brush.download_time) || 0,
            seed_avgspeed: Number(form.brush.seed_avgspeed) || 0,
            delete_size_range: (form.brush.delete_size_range || '').trim(),
            include: (form.brush.include || '').trim(),
            exclude: (form.brush.exclude || '').trim(),
            size: (form.brush.size || '').trim(),
            seeder: (form.brush.seeder || '').trim(),
            pubtime: (form.brush.pubtime || '').trim(),
            fail_backoff_minutes: Number(form.brush.fail_backoff_minutes) || 0,
            qb_category: (form.brush.qb_category || '').trim() || 'ptbrush',
            check_interval: Number(form.brush.check_interval) || 5,
        },
        sites: form.sites.map(s => ({
            name: s.name,
            type: s.type || '',
            base_url: s.base_url || '',
            rss_url: s.rss_url || '',
            cookie: s.cookie || '',
            headers: (s.headers || []).map(h => ({ key: h.key, value: h.value || '' })),
        })),
    };
}

export default defineComponent({
    name: 'Config',
    components: { SizeInput, WorkTimePicker },
    setup() {
        const form = reactive(emptyForm());
        const supportedSites = ref([]);
        const siteTypes = ref([]);
        const promotionTypes = ref([]);
        const loaded = ref(false);
        const loading = ref(false);
        const saving = ref(false);
        // 配置版本号：保存时回传；页面数据过期会被后端拒绝，避免覆盖别处的改动
        const rev = ref('');

        // 「参与刷流的站点」：空数组 = 全部站点参与（新增站点自动加入）；
        // 关掉任意站点时展开成显式列表，全部打开时再收回空数组。
        const siteKeyList = computed(() => (form.sites || []).map(s => siteKey(s)));
        // 站点开关按中文站名排序（中文排序规则，等同拼音序），与「站点配置」页的「按名称」一致
        const brushSiteList = computed(() => (form.sites || []).slice().sort(
            (a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN')
        ));
        // 只有用户亲手改过站点开关才置 true：后端据此判断「名单变窄」是不是有意的
        const targetsTouched = ref(false);

        // 低速删种分钟必须短于「低速种子不占位」的判定窗口，否则会先让位后删，可能超发
        const slowDeleteOverWindow = computed(() => {
            const minutes = Number(form.brush.slow_delete_minutes) || 0;
            const cycle = Number(form.brush.download_cycle) || 1200;
            return minutes > 0 && minutes * 60 >= cycle;
        });
        const downloadCycleMinutes = computed(() => Math.round((Number(form.brush.download_cycle) || 1200) / 60));

        function isTargeted(site) {
            if (form.brush.target_none) return false;
            const list = form.brush.target_sites || [];
            if (!list.length) return true;
            return list.includes(siteKey(site));
        }

        function toggleTarget(site, on) {
            targetsTouched.value = true;
            const key = siteKey(site);
            // 当前是「一个都不参与」时，先清掉这个标记再按开关算名单
            let list = form.brush.target_none ? [] : (form.brush.target_sites || []).slice();
            // 名单留空且不是「都不参与」= 全部参与，先展开成全部键再关掉这一个
            if (!form.brush.target_none && !list.length) list = siteKeyList.value.slice();
            if (on) {
                if (!list.includes(key)) list.push(key);
            } else {
                list = list.filter(k => k !== key);
            }
            const coveredAll = siteKeyList.value.every(k => list.includes(k));
            if (coveredAll) {
                // 全部打开就收回空数组，以后新加的站点自动加入
                form.brush.target_sites = [];
                form.brush.target_none = false;
            } else if (!list.length) {
                // 一个都不剩：必须显式标记「都不参与」，否则空数组会被当成「全部参与」
                form.brush.target_sites = [];
                form.brush.target_none = true;
            } else {
                form.brush.target_sites = list;
                form.brush.target_none = false;
            }
            // 开关改动当场保存：以前要用户再点「保存」才生效，一旦刷新页面就恢复成旧值，像是「自己又打开了」
            scheduleTargetSave();
        }

        let targetSaveTimer = null;
        function scheduleTargetSave() {
            if (targetSaveTimer) clearTimeout(targetSaveTimer);
            targetSaveTimer = setTimeout(persistTargets, 350);
        }

        async function persistTargets() {
            if (saving.value || !loaded.value) return;
            saving.value = true;
            try {
                const payload = stripPayload(form);
                payload.rev = rev.value;
                payload.targets_touched = true;
                const res = await api.put('/api/config', payload);
                if (res?.rev) rev.value = res.rev;
                targetsTouched.value = false;
                ElMessage.success(`已保存：${targetSummary.value}`);
            } catch (e) {
                ElMessage.error(e.message || '保存参与站点失败');
                await load();
            } finally {
                saving.value = false;
            }
        }

        const targetSummary = computed(() => {
            const total = (form.sites || []).length;
            if (form.brush.target_none) return `没有站点参与（0 / ${total}）`;
            const list = form.brush.target_sites || [];
            return list.length ? `仅 ${list.length} / ${total} 个站点参与` : `全部 ${total} 个站点参与`;
        });

        async function load() {
            loading.value = true;
            try {
                const data = await api.get('/api/config');
                rev.value = data.rev || '';
                targetsTouched.value = false;
                Object.assign(form.brush, data.brush);
                form.sites = data.sites || [];
                supportedSites.value = data.supported_sites || [];
                siteTypes.value = data.site_types || [];
                promotionTypes.value = data.promotion_types || [];
                loaded.value = true;
            } catch (e) {
                ElMessage.error(e.message || '加载失败');
            } finally {
                loading.value = false;
            }
        }

        async function save() {
            saving.value = true;
            try {
                const payload = stripPayload(form);
                payload.rev = rev.value;
                payload.targets_touched = targetsTouched.value;
                const res = await api.put('/api/config', payload);
                const n = (res?.sites_count ?? form.sites.length);
                ElMessage.success(`${res?.message || '已保存'}（共 ${n} 个站点）`);
                await load();
            } catch (e) {
                if (e.status === 409) {
                    // 页面拿的是旧配置：拒绝保存，把最新配置拉回来，避免覆盖别处的改动
                    ElMessage.error(e.message || '配置已过期，请刷新后重试');
                    await load();
                } else if (e.details?.length) {
                    const first = e.details[0];
                    ElMessage.error(`${first.loc?.join('.')}: ${first.msg}`);
                } else {
                    ElMessage.error(e.message || '保存失败');
                }
            } finally {
                saving.value = false;
            }
        }

        async function testSite(site) {
            try {
                const res = await api.post('/api/config/test-site', {
                    name: site.name,
                    type: site.type || '',
                    base_url: site.base_url || '',
                    rss_url: site.rss_url || '',
                    cookie: site.cookie,
                    headers: site.headers,
                });
                if (res?.ok) ElMessage.success(res.message);
                else ElMessage.error(res?.message || '站点测试失败');
            } catch (e) {
                ElMessage.error(e.message || '请求失败');
            }
        }

        // 一键关闭全部条件删种，回到「只按无上传/过期清理」的老行为
        function clearDeleteConditions() {
            form.brush.seed_time = 0;
            form.brush.hr_seed_time = 0;
            form.brush.seed_ratio = 0;
            form.brush.slow_up_delete_enabled = false;
            form.brush.slow_up_delete_minutes = 0;
        }

        // 默认值：本软件只做一件事——在免费期里尽快下完、尽量多上传，
        // 所以做种不必留太久，磁盘和并发位要快速周转给新热门种。
        // 运行一次：立刻跑一轮选种加种 + 删种检查（和定时任务同一条链路）
        const running = ref(false);

        async function runOnce() {
            running.value = true;
            try {
                const res = await api.post('/api/brush/run', {});
                const added = Number((res && res.added) || 0);
                const chk = (res && res.check) || {};
                const deleted = Number(chk.conditions || 0) + Number(chk.volume || 0) + Number(chk.slow_up || 0);
                if (added > 0) {
                    ElMessage.success(`已运行一轮：新增 ${added} 个种子，删种检查完成（删 ${deleted} 个）`);
                } else {
                    ElMessage.warning(`已运行一轮：本轮未加种，删种检查完成（删 ${deleted} 个），原因见仪表盘诊断`);
                }
            } catch (e) {
                ElMessage.error(e.message || '运行失败');
            } finally {
                running.value = false;
            }
        }

        // 促销类型多选：点「不限制」会独占，点具体类型会取消「不限制」
        function togglePromotion(value) {
            const cur = [...(form.brush.promotions || [])];
            if (value === 'all') {
                form.brush.promotions = cur.includes('all') ? ['free', 'free_2x'] : ['all'];
                return;
            }
            if (cur.includes('all')) {
                form.brush.promotions = [value];
                return;
            }
            if (cur.includes(value)) {
                const rest = cur.filter((v) => v !== value);
                form.brush.promotions = rest.length ? rest : ['free'];
            } else {
                form.brush.promotions = [...cur, value];
            }
        }

        onMounted(load);

        return {
            form, brushSiteList, supportedSites, siteTypes, promotionTypes, loaded, loading, saving,
            load, save, testSite, togglePromotion, siteKey,
            clearDeleteConditions, runOnce, running,
            isTargeted, toggleTarget, targetSummary, persistTargets,
            slowDeleteOverWindow, downloadCycleMinutes,
        };
    },
    template: `
        <div class="config-page">
            <div class="config-page-header">
                <div>
                    <h2>刷流配置</h2>
                    <div class="config-page-sub">
                        刷流条件与调度参数，保存后立即生效，不需要重启容器。
                    </div>
                </div>
                <div class="config-actions">
                    <el-button :loading="running" :disabled="!loaded" plain @click="runOnce" title="手动跑一轮：选种加种 + 删种检查">运行一次</el-button>
                    <el-button :loading="loading" plain @click="load">重新加载</el-button>
                    <el-button type="primary" :loading="saving" :disabled="!loaded" @click="save">保存</el-button>
                </div>
            </div>

            <div v-if="loading && !loaded" style="text-align:center;padding:48px;color:#909399;">
                加载中...
            </div>

            <template v-if="loaded">
                <el-card style="margin-bottom:16px;" shadow="hover">
                    <template #header><strong>参与刷流的站点</strong></template>
                    <el-form label-position="top">
                        <div class="form-grid">
                            <el-form-item class="form-full" >
                                <div class="site-toggle-list" style="width:100%;">
                                    <div
                                        v-for="s in brushSiteList"
                                        :key="siteKey(s)"
                                        class="site-toggle-row">
                                        <div class="site-toggle-main">
                                            <strong class="site-toggle-name">{{ s.name }}</strong>
                                            <span class="site-toggle-domain">{{ siteKey(s) }}</span>
                                            <el-tag v-if="s.enabled === false" size="small" type="info">已停用</el-tag>
                                        </div>
                                        <el-switch
                                            :model-value="isTargeted(s)"
                                            @change="toggleTarget(s, $event)" />
                                    </div>
                                    <div v-if="!form.sites.length" class="site-toggle-empty">
                                        还没有配置站点，先去「站点配置」页添加。
                                    </div>
                                </div>
                                <div class="field-help">
                                    开着的站点才会抓种、刷流；开关改动会立即保存。全部站点都打开时，新加的站点会自动加入；只打开部分站点时，新加的站点不会自动加入。
                                </div>
                            </el-form-item>
                        </div>
                    </el-form>
                </el-card>

                <el-card style="margin-bottom:16px;" shadow="hover">
                    <template #header><strong>选种条件</strong></template>
                    <el-form label-position="top">
                        <div class="form-grid">
                            <el-form-item class="form-full" label="只下载这些促销类型（可多选）">
                                <div class="promotion-row">
                                    <el-checkbox
                                        v-for="p in promotionTypes"
                                        :key="p.value"
                                        :model-value="form.brush.promotions.includes(p.value)"
                                        @change="togglePromotion(p.value)">
                                        {{ p.label }}
                                    </el-checkbox>
                                </div>
                                <div class="field-help">
                                    一般只勾「免费」和「2X免费」，下载不计下载量。想刷上传、又不心疼下载量的站，可以加勾「2X上传」或「50%下载」。勾「不限制」会连非促销种子一起下，纯烧下载量，除非你是大管宽且站不记下载量，否则不建议。
                                </div>
                            </el-form-item>
                            <el-form-item class="form-full" label="排除 H&amp;R（Hit and Run）种子">
                                <div class="switch-row">
                                    <el-switch v-model="form.brush.exclude_hr" />
                                    <span class="switch-hint">{{ form.brush.exclude_hr ? '已开启：带 H&R 标记的种子不下载' : '已关闭：H&R 种子也会下载' }}</span>
                                </div>
                                <div class="field-help">
                                    站点在列表页给种子打了 H&R 标记（图标或文字）时就不下载，避免下了不保种被记 H&R。注意：少数站只在详情页标 H&R，列表页看不出来，这种识别不到，保种时间仍需自己盯。
                                </div>
                            </el-form-item>
                            <el-form-item label="免费剩余时间不足（小时）不下载">
                                <el-input-number
                                    v-model="form.brush.min_free_hours"
                                    :min="0" :max="720"
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    免费种子剩余免费时间少于这个小时数就不再新增，避免刚下完免费就结束、后面白忙。填 0 表示不限制。
                                </div>
                            </el-form-item>
                            <el-form-item label="包含关键词（正则，留空不限制）">
                                <el-input v-model="form.brush.include" clearable placeholder="例如 2160p|HDR，标题必须匹配才下载" />
                                <div class="field-help">
                                    对种子标题做正则匹配，不区分大小写。写了就只下命中的种子，多个关键词用 <code>|</code> 分隔。正则写错保存时会直接报错。
                                </div>
                            </el-form-item>
                            <el-form-item label="排除关键词（正则，留空不排除）">
                                <el-input v-model="form.brush.exclude" clearable placeholder="例如 1080p|DV|国语，命中就不下" />
                                <div class="field-help">
                                    标题命中这个正则就跳过。常用来避开不想要的规格或重编码版本，优先级高于包含关键词。
                                </div>
                            </el-form-item>
                            <el-form-item label="种子体积范围（GiB）">
                                <el-input v-model="form.brush.size" clearable placeholder="例如 10 或 10-80" />
                                <div class="field-help">
                                    单值表示下限：填 <code>10</code> 表示只下 10 GiB 以上的。区间表示两端都卡：<code>10-80</code> 表示只下 10 GiB 到 80 GiB 之间。留空不限制。这是选种门槛：整包体积不在区间内的种子不会被加入。太小的包没上传可抢，太大的包会长时间占着并发位。
                                </div>
                            </el-form-item>
                            <el-form-item label="做种人数范围">
                                <el-input v-model="form.brush.seeder" clearable placeholder="例如 30 或 10-50" />
                                <div class="field-help">
                                    单值表示上限：填 <code>30</code> 表示做种人数超过 30 就不下（人太多抢不到上传）。区间表示两端都卡：<code>10-50</code>。留空不限制。
                                </div>
                            </el-form-item>
                            <el-form-item label="发布时间范围（分钟）">
                                <el-input v-model="form.brush.pubtime" clearable placeholder="例如 60 或 10-120" />
                                <div class="field-help">
                                    只下新鲜的种子。单值表示上限：填 <code>60</code> 表示只下 60 分钟内发布的。区间表示两端都卡：<code>10-120</code>。留空不限制。
                                </div>
                            </el-form-item>
                        </div>
                    </el-form>
                </el-card>

                <el-card style="margin-bottom:16px;" shadow="hover">
                    <template #header><strong>刷流调度</strong></template>
                    <el-form label-position="top">
                        <div class="form-grid">
                            <el-form-item label="刷流间隔（分钟）">
                                <el-input-number
                                    v-model="form.brush.brush_interval"
                                    :min="5" :max="1440" :step="1" step-strictly
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    每隔这么多分钟检查一次并决定要不要新增刷流种子。最小 5 分钟（填小于 5 的值会按 5 处理），最大 1440 分钟（一天）。保存后立即生效，不用重启容器。它只决定“多久看一次”，真正能不能加种还受工作时间、同时下载数上限、剩余磁盘空间等条件约束。候选种子库按同一间隔抓取，两者同频，所以新种从发布到被挑走取决于两轮检查的间隔。
                                </div>
                            </el-form-item>
                                                        <el-form-item label="最小剩余磁盘空间">
                                <SizeInput v-model="form.brush.min_disk_space" />
                                <div class="field-help">
                                    指 qBittorrent 通过 Web API 返回的剩余空间，通常是 qBittorrent 下载目录所在磁盘或 Docker 挂载卷。剩余空间低于这个值就只停止新增，已存在的任务仍按清理条件处理。如果种子体积上限接近或大于这个值，建议把下限调到“最大种子体积 + 10%～20% 余量”，否则磁盘快满时仍可能挑进一个装不下的包。
                                </div>
                            </el-form-item>
                            <el-form-item label="最大同时下载种子数">
                                <el-input-number
                                    v-model="form.brush.max_downloading_torrents"
                                    :min="1" :max="100"
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    只统计本工具加入到 qBittorrent、并且真的在下种的未完成任务。显示「等待」、排队、暂停的种子不占名额，下载速度一直很低的也不占（见下一项）。达到上限后不再新增，已完成或其他分类的种子不计入。多个下载器上的刷流种子合并计数。
                                </div>
                            </el-form-item>
                            <el-form-item label="低速种子不占下载位（MiB/s）">
                                <el-input-number
                                    v-model="form.brush.download_speed_min"
                                    :min="0" :max="200" :step="0.1" :precision="1"
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    未完成种子的下载速度低于这个值、并且持续一个判定窗口以上，就不再占用「最大同时下载种子数」的名额，让新种子可以补进来。这个值约半条线的下行带宽：定得比带宽还高，位会被周期性全部腾空、并发超发，下载反而摊薄。这一步故意排在「低速下载持续就删」后面：死种先被删掉、位随删释放，只有「活着但偏慢」的种子才靠这条让位。填 0 表示关闭这条判断（只看状态，等待中/排队/暂停照样不占位）。
                                </div>
                            </el-form-item>
                            <el-form-item label="当前工作时间字符串">
                                <el-input :model-value="form.brush.work_time || '（24 小时）'" disabled />
                                <div class="field-help">
                                    由下方小时格自动生成并写入配置文件。显示为“24 小时”时，表示不限制新增刷流任务的时间段。
                                </div>
                            </el-form-item>
                            <el-form-item class="form-full" label="工作时间（点击小时格切换）">
                                <WorkTimePicker v-model="form.brush.work_time" />
                                <div class="field-help">
                                    绿色格子表示该小时允许新增刷流任务；不选任何小时则全天允许。它只限制“新增刷流”，状态采集、清理和已有任务不会因此停止。
                                </div>
                            </el-form-item>
                        </div>
                    </el-form>
                </el-card>

                <el-card style="margin-bottom:16px;" shadow="hover">
                    <template #header>
                        <div style="display:flex;align-items:center;justify-content:space-between;">
                            <strong>删种条件</strong>
                            <el-button size="small" plain @click="clearDeleteConditions">全部关闭</el-button>
                        </div>
                    </template>
                    <el-form label-position="top">
                        <div class="form-grid">
                            <el-form-item label="做种时长达到（小时）就删">
                                <el-input-number
                                    v-model="form.brush.seed_time"
                                    :min="0" :max="720" :step="1" :precision="1"
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    种子下载完成后做种满这么多小时就删除。填 0 表示不按做种时长删。这是「做够了就走」的主力条件，配合下面的分享率一起决定种子何时回收。
                                </div>
                            </el-form-item>
                            <el-form-item label="H&amp;R 种子做种时长达到（小时）才删">
                                <el-input-number
                                    v-model="form.brush.hr_seed_time"
                                    :min="0" :max="720" :step="1" :precision="1"
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    带 H&amp;R 标记的种子走单独分支，只按这一条和下面的分享率回收，不会套用普通种子的其他条件。填 0 表示 H&amp;R 种子不做时长回收（最稳妥，靠站点要求的保种期自己盯）。
                                </div>
                            </el-form-item>
                            <el-form-item label="分享率达到就删（仅已完成种子）">
                                <el-input-number
                                    v-model="form.brush.seed_ratio"
                                    :min="0" :max="100" :step="0.1" :precision="2"
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    下载完成的种子分享率达到这个值就删除。未下完的种子不看分享率。填 1.0 时，实测做种 12 小时内只到 0.1~0.4，填 3.0 基本永不触发；这条只当暴涨种的兜底。填 0 表示不按分享率删。
                                </div>
                            </el-form-item>
                            <el-form-item label="低速下载持续（分钟）就删">
                                <div class="number-action-row row-wrap">
                                    <el-input-number
                                        v-model="form.brush.slow_delete_speed"
                                        :min="0" :max="200" :step="0.1" :precision="1"
                                        controls-position="right" style="max-width:110px;" />
                                    <span class="row-unit">MiB/s 以下</span>
                                    <el-input-number
                                        v-model="form.brush.slow_delete_minutes"
                                        :min="0" :max="1440" :step="5"
                                        controls-position="right" style="max-width:110px;" />
                                    <span class="row-unit">分钟</span>
                                </div>
                                <div class="field-help">
                                    只管下载：没下完的种子，下载速度低于这个值、或者一直卡在「等待/排队」没能开始下载，持续这么多分钟就删掉，让位给新种。不看上传速度，也不会因为分享率低就删。自己一套阈值，和上面「低速种子不占下载位」互不影响；计时从掉速/进入等待那一刻算，中途速度回升或开始下载就清零。这个分钟数必须短于上面的判定窗口，否则会先让出下载位、种子却还留着，可能造成并发超发；填 0 分钟表示不删。H&R 种子不参与这条。
                                    <div v-if="slowDeleteOverWindow" style="color:#e6a23c;font-weight:600;margin-top:6px;">
                                        当前低速删种 {{ form.brush.slow_delete_minutes }} 分钟，不短于「低速种子不占下载位」的 {{ downloadCycleMinutes }} 分钟窗口；会先让出下载位、种子还没删，可能并发超发。建议把删种分钟改到窗口以内。
                                    </div>
                                </div>
                            </el-form-item>
                            <el-form-item label="低速上传持续（分钟）就删（盒子专用）">
                                <div style="display:flex;align-items:center;gap:10px;width:100%;margin-bottom:8px;">
                                    <el-switch
                                        v-model="form.brush.slow_up_delete_enabled"
                                        active-text="启用"
                                        inactive-text="关闭" />
                                    <span class="row-unit">
                                        {{ form.brush.slow_up_delete_enabled ? '已启用：下完的种达到条件就删' : '关闭：不判任何种子' }}
                                    </span>
                                </div>
                                <div class="number-action-row row-wrap">
                                    <el-input-number
                                        v-model="form.brush.slow_up_delete_speed"
                                        :min="0" :max="200" :step="0.05" :precision="2"
                                        :disabled="!form.brush.slow_up_delete_enabled"
                                        controls-position="right" style="width:86px;min-width:86px;" />
                                    <span class="row-unit">MiB/s 以下</span>
                                    <el-input-number
                                        v-model="form.brush.slow_up_delete_minutes"
                                        :min="0" :max="1440" :step="5"
                                        :disabled="!form.brush.slow_up_delete_enabled"
                                        controls-position="right" style="width:78px;min-width:78px;" />
                                    <span class="row-unit">分钟</span>
                                </div>
                                <div class="field-help">
                                    只管上传，且<b>只监测已经下载完成的种子</b>：下载中的种上传速度低是正常的（数据还没凑齐），拿它当下的速度判会误删抢手种；已下完的种挂着却只跑 0.0x MiB/s，才说明真没人要。上传速度低于这个值并持续这么多分钟，就删掉腾出保种位和带宽。盒子（seedbox）位子有限，一个种挂着只跑 0.0x MiB/s，留着纯占坑，不如换新热门种。计时从掉速那一刻算，中途上传回升就清零；下载侧的死种另有「低速下载持续就删」管，两条互不影响。H&amp;R 种子跳过，不会被这条提前删掉。<b>左侧是这条逻辑的总开关</b>：关着的时候完全不判，两个数字只是记住的草稿（灰掉不可改）；打开后还要求阈值和分钟都大于 0 才真删，双保险。实际判定精度取决于检查周期，分钟数建议填检查周期的整数倍。
                                </div>
                            </el-form-item>
                            <el-form-item label="无上传超时（分钟）">
                                <div class="number-action-row">
                                    <el-input-number
                                        v-model="form.brush.max_no_activate_time"
                                        :min="0"
                                        controls-position="right" />
                                </div>
                                <div class="field-help">
                                    只看上传速度：已经下完的种子连续这么多分钟上传速度都是 0，就认定没人拉了，删掉腾位置。正在下载的种子不受这条影响（它们归「低速下载持续就删」管）。填 0 关闭：没上传的种交给「低速下载持续就删」和「免费结束前就删」处理。1-4 分钟会按 5 分钟处理，防止误删短暂没速度的任务。
                                </div>
                            </el-form-item>
                            <el-form-item label="免费时间结束前就删">
                                <el-select v-model="form.brush.free_end_delete_minutes" style="width:100%;">
                                    <el-option :value="0" label="关闭（不提前删）" />
                                    <el-option :value="10" label="提前 10 分钟" />
                                    <el-option :value="20" label="提前 20 分钟" />
                                    <el-option :value="30" label="提前 30 分钟" />
                                </el-select>
                                <div class="field-help">
                                    没下完的种子，在免费期结束前这么多分钟就删掉：免费一过流量开始计入下载量，半截文件继续挂着只会白烧分享率。没写免费截止时间的不动；H&amp;R 种子不参与（照旧留着下完做种）。
                                </div>
                            </el-form-item>
                            <div class="field-help form-full">
                                以上条件按顺序检查，命中任意一条即删除；H&amp;R 种子只走它自己的两条。全部填 0 等于关闭条件删种，只保留本卡的「低速上传持续就删」「无上传超时」「低速下载持续就删」「免费结束前删种」几条老逻辑。
                                <br />
                                删种一律是「种子 + 已下载的数据文件」一起删（含未下载完的临时文件），不是只从下载器里摘掉种子。
                                删之前会先向站点重新汇报一次做种数据，再从下载器删除；删完会反查确认种子确实不在了，不确定就重试一次，仍失败会记一条 ERROR 日志。
                            </div>
                        </div>
                    </el-form>
                </el-card>

                <el-card style="margin-bottom:16px;" shadow="hover">
                    <template #header><strong>运行</strong></template>
                    <el-form label-position="top">
                        <div class="form-grid">
                            <el-form-item label="检查周期（分钟）">
                                <el-input-number
                                    v-model="form.brush.check_interval"
                                    :min="1" :max="1440" :step="1" step-strictly
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    删种与状态检查单独用这个周期，跟上面的刷流间隔互不影响。每一轮会一起跑「免费结束前清理 + 无上传清理 + 低速删种 + 条件删种」。它通常比刷流间隔跑得更勤，种子下不动或一直等待时能更快腾出并发位。修改后保存即时生效。
                                </div>
                            </el-form-item>
                            <el-form-item label="加种失败退避（分钟）">
                                <el-input-number
                                    v-model="form.brush.fail_backoff_minutes"
                                    :min="0" :max="43200" :step="60"
                                    controls-position="right" style="width:100%;" />
                                <div class="field-help">
                                    某个候选加种失败（取下载链接失败、种子内容下载失败、下载器拒收）后，这么多分钟内不再重试它，避免反复失败反复烧站点下载额度。填 0 表示不限制。
                                </div>
                            </el-form-item>
                            <el-form-item label="qBittorrent 分类名">
                                <el-input v-model="form.brush.qb_category" placeholder="ptbrush" />
                                <div class="field-help">
                                    本工具加入的种子统一打这个分类，删种、统计、同时下载数都只认这个分类。改名前请确认下载器里没有同名分类，改完后老分类的种子不归本工具管。
                                </div>
                            </el-form-item>
                        </div>
                    </el-form>
                </el-card>

                <div class="config-bottom-actions">
                    <el-button :loading="loading" plain @click="load">重新加载</el-button>
                    <el-button type="primary" :loading="saving" :disabled="!loaded" @click="save">保存</el-button>
                </div>
            </template>
        </div>
    `,
});
