import { defineComponent, ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';

export default defineComponent({
    name: 'QuickStart',
    setup() {
        const router = useRouter();
        const summary = ref({
            sites: { total: 0, cookie_ok: 0 },
            downloaders: { total: 0, alias: '' },
            tasks: { total: 0, enabled: 0 },
            rules: { total: 0, enabled: 0, select: 0, delete: 0 },
            steps: [],
        });
        const presets = ref([]);
        const loading = ref(false);
        const importing = ref('');
        const preview = ref(null);
        const previewVisible = ref(false);
        const previewLoading = ref(false);

        const pending = computed(() => (summary.value.steps || []).filter(s => !s.done));
        const heroTitle = computed(() => {
            if (!summary.value.sites.total) return '先加你自己的站点，再套规则';
            if (!summary.value.rules.total) return '选一套规则，3 分钟跑起来';
            return '规则已就位，先干跑再开闸';
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
            } catch (e) {
                ElMessage.error(e.message || '加载失败');
            } finally {
                loading.value = false;
            }
        }

        async function openPreview(pack) {
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

        async function doImport(pack) {
            importing.value = pack.id;
            try {
                const res = await api.post(`/api/presets/${pack.id}/import`, {});
                ElMessage.success(`已导入 ${res.created} 条规则（默认停用），任务站点按你现有的 ${res.sites.length} 个站点生成`);
                await load();
                if (previewVisible.value) previewVisible.value = false;
            } catch (e) {
                ElMessage.error(e.message || '导入失败');
            } finally {
                importing.value = '';
            }
        }

        function go(to) {
            if (to) router.push(to);
        }

        onMounted(load);
        return {
            summary, presets, loading, importing, preview, previewVisible, previewLoading,
            pending, heroTitle, load, openPreview, doImport, go,
        };
    },
    template: `
        <div class="sites-page qs-page" v-loading="loading">
            <div class="sites-header">
                <div>
                    <div class="sites-title">快速开始</div>
                    <div class="sites-sub">
                        站点数量由你自己决定：预设只装规则模板，不绑定站点。
                    </div>
                </div>
                <el-button @click="load">刷新</el-button>
            </div>

            <div class="qs-hero">
                <div class="qs-hero-main">
                    <div class="qs-kicker">QUICK START</div>
                    <div class="qs-h1">{{ heroTitle }}</div>
                    <div class="qs-p">
                        预设只提供规则模板，不绑定站点。加完你自己的站点，规则自动套用；
                        地址、Cookie 这些只有你知道的，导入后再补。
                    </div>
                </div>
                <div class="qs-stats">
                    <div class="qs-stat"><b>{{ summary.downloaders.total }}</b><span>下载器</span></div>
                    <div class="qs-stat"><b>{{ summary.sites.total }}</b><span>你添加的站点</span></div>
                    <div class="qs-stat"><b>{{ summary.tasks.total }}</b><span>任务</span></div>
                </div>
            </div>

            <div class="qs-section-head">
                <div class="qs-h2">选择一套套餐 <span class="qs-muted">预设 {{ presets.length }} 套</span></div>
                <div class="qs-muted">导入的规则默认「停用」，先干跑确认再启用</div>
            </div>

            <div class="qs-packs">
                <div class="qs-pack" v-for="p in presets" :key="p.id" :class="{ rec: p.badge === '推荐' }">
                    <div class="qs-badge" v-if="p.badge">{{ p.badge }}</div>
                    <div class="qs-pack-h">{{ p.name }}</div>
                    <div class="qs-pack-p">{{ p.desc }}</div>
                    <div class="qs-chips">
                        <span class="qs-chip blue">不绑站点</span>
                        <span class="qs-chip">选种 {{ p.counts.select }} 条</span>
                        <span class="qs-chip">删种 {{ p.counts.delete }} 条</span>
                        <span class="qs-chip">任务按站点生成</span>
                    </div>
                    <div class="qs-acts">
                        <el-button type="primary" :loading="importing === p.id" @click="doImport(p)">
                            {{ p.badge === '推荐' ? '导入推荐' : '导入' }}
                        </el-button>
                        <el-button :loading="previewLoading" @click="openPreview(p)">预览</el-button>
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
                                { t: '规则按套餐装好，默认停用', d: '选种 / 删种条件都能逐条改，改完在策略页干跑。', tag: '' },
                                { t: '任务指向你的下载器', d: '站点、分类、每小时上限都在任务里，导入时按你勾选的站点生成。', tag: '' },
                                { t: '先干跑，再开闸', d: '拿当前做种列表跑一遍规则，看会删谁、为什么，确认无误再启用。', tag: '' },
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

            <div class="qs-foot">PTBrush v1.2.1 · 规则引擎预览（44446）</div>

            <el-dialog v-model="previewVisible" title="导入预览" width="520px">
                <div v-if="preview">
                    <div class="qs-preview-line">
                        将套用到 <b>{{ preview.site_count }}</b> 个站点（你自己现有的站点），
                        新增 <b>{{ preview.to_add }}</b> 条规则，冲突 <b>{{ preview.conflicts }}</b> 条。
                    </div>
                    <div class="qs-preview-item" v-for="(it, i) in preview.items" :key="i">
                        <el-tag size="small" :type="it.kind === 'select' ? 'primary' : 'danger'" effect="plain">
                            {{ it.kind === 'select' ? '选种' : '删种' }}
                        </el-tag>
                        <span class="qs-preview-name">{{ it.name }}</span>
                        <el-tag v-if="it.conflict" size="small" type="warning">已存在，跳过</el-tag>
                    </div>
                </div>
                <template #footer>
                    <el-button @click="previewVisible = false">取消</el-button>
                    <el-button type="primary" @click="doImport({ id: preview.preset.id })">确认导入（默认停用）</el-button>
                </template>
            </el-dialog>
        </div>
    `,
});
