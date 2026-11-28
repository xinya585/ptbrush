import { defineComponent, ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api.js';

export default defineComponent({
    name: 'QuickStart',
    setup() {
        const router = useRouter();
        const summary = ref({
            sites: { total: 0, cookie_ok: 0 },
            downloaders: { total: 0, alias: '' },
            brush: { preset_id: '', preset_name: '', matched: false, params: 0 },
            steps: [],
        });
        const presets = ref([]);
        const selectedId = ref('');
        const loading = ref(false);
        const importing = ref('');
        const preview = ref(null);
        const previewVisible = ref(false);
        const previewLoading = ref(false);

        const pending = computed(() => (summary.value.steps || []).filter(s => !s.done));
        const appliedId = computed(() => (summary.value.brush && summary.value.brush.preset_id) || '');
        const heroTitle = computed(() => {
            if (!summary.value.sites.total) return '先加你自己的站点，再套刷流参数';
            if (!summary.value.brush.matched) return '选一套参数，3 分钟跑起来';
            return '刷流参数已就位，按需微调';
        });

        async function load() {
            loading.value = true;
            try {
                const [s, p] = await Promise.all([
                    api.get('/api/quickstart/summary'),
                    api.get('/api/presets'),
                ]);
                summary.value = s;
                presets.value = p.presets || [];
                selectedId.value = (s.brush && s.brush.preset_id) || '';
            } catch (e) {
                ElMessage.error(e.message || '加载失败');
            } finally {
                loading.value = false;
            }
        }

        function selectPack(pack) {
            selectedId.value = pack.id;
        }

        async function openPreview(pack) {
            selectedId.value = pack.id;
            previewLoading.value = true;
            try {
                preview.value = await api.post(`/api/presets/${pack.id}/preview`, {});
                previewVisible.value = true;
            } catch (e) {
                ElMessage.error(e.message || '预览失败');
            } finally {
                previewLoading.value = false;
            }
        }

        async function doImport(pack, skipConfirm = false) {
            selectedId.value = pack.id;
            if (!skipConfirm) {
                try {
                    await ElMessageBox.confirm(
                        `会覆盖刷流配置里的促销、体积、做种、删种条件等参数；站点、下载器、保存目录、分类名不受影响。是否继续？`,
                        `套用「${pack.name}」`,
                        { type: 'warning', confirmButtonText: '套用参数', cancelButtonText: '取消' }
                    );
                } catch {
                    return;
                }
            }
            importing.value = pack.id;
            try {
                const res = await api.post(`/api/presets/${pack.id}/import`, {});
                const changed = res.changed ? `更新 ${res.changed} 项` : '当前配置本来就一致';
                ElMessage.success(`已套用「${res.preset.name}」：${changed}（共 ${res.total} 项）`);
                await load();
                if (previewVisible.value) previewVisible.value = false;
            } catch (e) {
                ElMessage.error(e.message || '套用失败');
            } finally {
                importing.value = '';
            }
        }

        function go(to) {
            if (to) router.push(to);
        }

        onMounted(load);
        return {
            summary, presets, selectedId, appliedId, loading, importing, preview, previewVisible, previewLoading,
            pending, heroTitle, load, selectPack, openPreview, doImport, go,
        };
    },
    template: `
        <div class="sites-page qs-page" v-loading="loading">
            <div class="sites-header">
                <div>
                    <div class="sites-title">快速开始</div>
                    <div class="sites-sub">
                        三套套餐只改「刷流配置」里的简单参数，不产生额外配置。
                    </div>
                </div>
                <el-button @click="load">刷新</el-button>
            </div>

            <div class="qs-hero">
                <div class="qs-hero-main">
                    <div class="qs-kicker">QUICK START</div>
                    <div class="qs-h1">{{ heroTitle }}</div>
                    <div class="qs-p">
                        不绑站点，也不碰你的下载器和保存目录。选一套套餐，把促销、体积、做种、删种条件
                        这些数值一次性填好；后面想改，直接去「刷流配置」逐项微调。
                    </div>
                </div>
                <div class="qs-stats">
                    <div class="qs-stat"><b>{{ summary.downloaders.total }}</b><span>下载器</span></div>
                    <div class="qs-stat"><b>{{ summary.sites.total }}</b><span>你添加的站点</span></div>
                    <div class="qs-stat"><b class="qs-stat-text">{{ summary.brush.preset_name || '未套用' }}</b><span>当前套餐</span></div>
                </div>
            </div>

            <div class="qs-section-head">
                <div class="qs-h2">选择一套套餐 <span class="qs-muted">预设 {{ presets.length }} 套</span></div>
                <div class="qs-muted">蓝框是当前已生效的套餐；浅色框是你正在查看的套餐</div>
            </div>

            <div class="qs-packs">
                <div class="qs-pack" v-for="p in presets" :key="p.id"
                    :class="{ active: appliedId === p.id, selected: selectedId === p.id && appliedId !== p.id }" @click="selectPack(p)">
                    <div class="qs-pack-h">{{ p.name }}</div>
                    <div class="qs-pack-p">{{ p.desc }}</div>
                    <div class="qs-chips">
                        <span class="qs-chip blue">不绑站点</span>
                        <span class="qs-chip">参数 {{ p.params }} 项</span>
                        <span class="qs-chip" v-for="(h, i) in (p.highlights || [])" :key="i">{{ h }}</span>
                    </div>
                    <div class="qs-acts">
                        <el-button type="primary" :loading="importing === p.id" @click.stop="doImport(p)">
                            导入
                        </el-button>
                        <el-button :loading="previewLoading" @click.stop="openPreview(p)">预览</el-button>
                    </div>
                </div>
            </div>

            <el-row :gutter="16" class="qs-lower">
                <el-col :xs="24" :md="14">
                    <el-card shadow="hover">
                        <template #header>
                            <div class="qs-card-h">
                                <span>导入之后会发生什么</span>
                                <el-tag size="small" type="success">不写你的数据</el-tag>
                            </div>
                        </template>
                        <div class="qs-steps">
                            <div class="qs-step" v-for="(s, i) in [
                                { t: '站点不用先凑齐', d: '加 1 个还是 30 个都行；认识的站自动带出解析器和免费口径，不认识的站手填域名也能用。', tag: '按你的来' },
                                { t: '套餐直接改刷流配置', d: '只覆盖促销、体积、做种人数、删种条件、检查间隔这些数值，保存后立即生效。', tag: '' },
                                { t: '数值随时能逐项微调', d: '套用后去「刷流配置」改任意一项，保存就生效；套餐只是帮你填一套起步值。', tag: '' },
                                { t: '盒子专用开关单独处理', d: '只有「大硬盘盒子」默认打开低速上传持续就删；另外两套关闭，分钟和阈值只作为草稿保留，不会真删。', tag: '低速上传' },
                            ]" :key="i">
                                <div class="qs-num">{{ i + 1 }}</div>
                                <div class="qs-step-body">
                                    <div class="qs-step-h">{{ s.t }} <el-tag v-if="s.tag" size="small" type="warning" effect="plain">{{ s.tag }}</el-tag></div>
                                    <div class="qs-step-p">{{ s.d }}</div>
                                </div>
                            </div>
                        </div>
                    </el-card>
                </el-col>
                <el-col :xs="24" :md="10">
                    <el-card shadow="hover" class="qs-todo-card">
                        <template #header>
                            <div class="qs-card-h">
                                <span>还差 {{ pending.length }} 步就能跑</span>
                                <el-tag size="small" type="warning" effect="plain">{{ pending.length }} 项待办</el-tag>
                            </div>
                        </template>
                        <div class="qs-todo">
                            <div class="qs-todo-row" v-for="s in summary.steps" :key="s.key" @click="go(s.to)">
                                <span class="qs-dot" :class="{ ok: s.done }"></span>
                                <b>{{ s.label }}</b>
                                <span class="qs-todo-detail">{{ s.detail }}</span>
                            </div>
                        </div>
                        <div class="qs-note">
                            不知道 Cookie 怎么取？站点卡片上有「获取 Cookie」按钮，会打开站点登录页并在你登录后自动回填。
                        </div>
                    </el-card>
                </el-col>
            </el-row>

            <div class="qs-foot">PTBrush v1.2.3</div>

            <el-dialog v-model="previewVisible" title="参数预览" width="640px">
                <div v-if="preview">
                    <div class="qs-preview-line">
                        将更新 <b>{{ preview.changed }}</b> 项，共 <b>{{ preview.total }}</b> 项刷流参数。
                        站点、下载器、保存目录、工作时间和 qBittorrent 分类名都会保留。
                    </div>
                    <div class="qs-preview-empty" v-if="!preview.items.length">
                        当前刷流配置已经和「{{ preview.preset.name }}」一致，导入不会改动任何参数。
                    </div>
                    <div class="qs-preview-list" v-else>
                        <div class="qs-preview-row" v-for="(it, i) in preview.items" :key="i">
                            <span class="qs-preview-name">{{ it.label }}</span>
                            <span class="qs-preview-old">{{ it.current }}</span>
                            <span class="qs-preview-arrow">→</span>
                            <span class="qs-preview-new">{{ it.target }}</span>
                        </div>
                    </div>
                </div>
                <template #footer>
                    <el-button @click="previewVisible = false">取消</el-button>
                    <el-button type="primary" :disabled="!preview || !preview.changed"
                        @click="doImport({ id: preview.preset.id, name: preview.preset.name }, true)">
                        应用这些参数
                    </el-button>
                </template>
            </el-dialog>
        </div>
    `,
});
