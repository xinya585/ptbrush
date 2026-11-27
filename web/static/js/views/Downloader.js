import { defineComponent, reactive, ref, onMounted, computed } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';

/**
 * 下载器配置页。
 *
 * 维护 config.toml 里的 [[downloaders]] 数组，可以配多个 qBittorrent，
 * 每个带一个别名；站点编辑里的「下载器」下拉就是这个别名列表。
 * 读写走 /api/downloaders，不碰刷流规则和站点配置。
 */

function emptyItem(idx) {
    return {
        alias: idx === 1 ? '默认' : `下载器${idx}`,
        url: '',
        auth_type: 'password',
        username: '',
        password: '',
        api_key: '',
        // 该下载器的默认保存路径：站点没单独填就用它
        save_path: '',
        // 仅前端用：这条的测试状态
        testing: false,
        testResult: '',
        testOk: null,
    };
}

function normalize(raw, idx) {
    return {
        ...emptyItem(idx),
        alias: (raw && raw.alias) || (idx === 1 ? '默认' : `下载器${idx}`),
        url: (raw && raw.url) || '',
        auth_type: (raw && raw.auth_type) || 'password',
        username: (raw && raw.username) || '',
        password: (raw && raw.password) || '',
        api_key: (raw && raw.api_key) || '',
        save_path: (raw && raw.save_path) || '',
    };
}

export default defineComponent({
    name: 'Downloader',
    setup() {
        const items = ref([]);
        const loading = ref(false);
        const saving = ref(false);

        const isEmpty = computed(() => items.value.length === 0);

        async function load() {
            loading.value = true;
            try {
                const data = await api.get('/api/downloaders');
                const list = (data && data.downloaders) || [];
                items.value = list.map((d, i) => normalize(d, i + 1));
            } catch (e) {
                ElMessage.error(e.message || '读取下载器配置失败');
            } finally {
                loading.value = false;
            }
        }

        function addItem() {
            items.value.push(emptyItem(items.value.length + 1));
        }

        function removeItem(index) {
            items.value.splice(index, 1);
        }

        function payloadOf(item, idx) {
            return {
                alias: (item.alias || '').trim() || `下载器${idx + 1}`,
                url: (item.url || '').trim(),
                auth_type: item.auth_type || 'password',
                username: item.username || '',
                password: item.password || '',
                api_key: item.api_key || '',
                save_path: (item.save_path || '').trim(),
            };
        }

        async function save() {
            const payloads = items.value.map((it, i) => payloadOf(it, i));
            const bad = payloads.findIndex((p) => !p.url);
            if (bad >= 0) {
                ElMessage.warning(`第 ${bad + 1} 个下载器还没填地址`);
                return;
            }
            const dup = new Set();
            for (const p of payloads) {
                if (dup.has(p.alias)) {
                    ElMessage.warning(`别名「${p.alias}」重复了，每个下载器要不同的名字`);
                    return;
                }
                dup.add(p.alias);
            }
            saving.value = true;
            try {
                const res = await api.put('/api/downloaders', { downloaders: payloads });
                if (res && res.downloaders) {
                    items.value = res.downloaders.map((d, i) => normalize(d, i + 1));
                }
                ElMessage.success((res && res.message) || '下载器配置已保存');
            } catch (e) {
                ElMessage.error(e.message || '保存失败');
            } finally {
                saving.value = false;
            }
        }

        async function test(index) {
            const item = items.value[index];
            if (!item || !(item.url || '').trim()) {
                ElMessage.warning('请先填 qBittorrent 地址');
                return;
            }
            item.testing = true;
            item.testResult = '';
            item.testOk = null;
            try {
                const res = await api.post('/api/config/test-downloader', payloadOf(item, index));
                item.testOk = !!(res && res.ok);
                item.testResult = (res && res.message) || (item.testOk ? '连接成功' : '连接失败');
            } catch (e) {
                item.testOk = false;
                item.testResult = e.message || '请求失败';
            } finally {
                item.testing = false;
            }
        }

        onMounted(load);

        return {
            items,
            loading,
            saving,
            isEmpty,
            load,
            addItem,
            removeItem,
            save,
            test,
        };
    },
    template: `
        <div class="sites-page">
            <div class="sites-header">
                <div>
                    <div class="sites-title">下载器配置</div>
                    <div class="sites-sub">
                        可以配多个 qBittorrent，每个带一个别名；站点编辑里的「下载器」就按别名选。
                        第一个是主下载器，剩余空间/任务数/限速按它判断。
                    </div>
                </div>
                <div class="sites-actions">
                    <el-button @click="addItem">增加下载器</el-button>
                    <el-button type="primary" :loading="saving" @click="save">保存</el-button>
                </div>
            </div>

            <div v-if="isEmpty && !loading" class="sites-empty">
                <div class="sites-empty-title">还没有配置下载器</div>
                <div class="sites-empty-sub">点右上角「增加下载器」，填 qBittorrent 的 WebUI 地址和账号就能开始刷流。</div>
            </div>

            <el-card
                v-for="(item, index) in items"
                :key="index"
                shadow="never"
                style="margin-bottom:16px;"
                v-loading="loading">

                <template #header>
                    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
                        <div style="display:flex;align-items:center;gap:8px;">
                            <strong>{{ item.alias || ('下载器' + (index + 1)) }}</strong>
                            <span v-if="index === 0" style="font-size:12px;color:#909399;">主下载器</span>
                        </div>
                        <div style="display:flex;gap:8px;">
                            <el-button size="small" :loading="item.testing" @click="test(index)">测试连接</el-button>
                            <el-button size="small" type="danger" plain @click="removeItem(index)">删除</el-button>
                        </div>
                    </div>
                </template>

                <el-form label-position="top">
                    <div class="form-grid">
                    <el-form-item label="别名">
                        <el-input v-model="item.alias" placeholder="例如 主力qB" />
                        <div class="field-help">
                            站点编辑里的「下载器」下拉按这个名字选；改完记得去站点里重新选一次。
                        </div>
                    </el-form-item>

                    <el-form-item label="qBittorrent 地址">
                        <el-input v-model="item.url" placeholder="http://127.0.0.1:8080" clearable />
                        <div class="field-help">
                            容器是 host 网络，qB 在同一台 NAS 上可填 http://127.0.0.1:&lt;端口&gt;；
                            也可以填局域网地址，如 http://192.168.1.2:8080
                        </div>
                    </el-form-item>

                    <el-form-item label="保存路径">
                        <el-input v-model="item.save_path" placeholder="例如 /downloads 或 /downloads/刷流" clearable />
                        <div class="field-help">
                            这个下载器的默认保存目录（qB 所在机器上的绝对路径）。站点编辑里没单独填保存路径时，种子就落这里；
                            留空则用 qB 的分类路径（默认下载目录/_ptbrush）。
                        </div>
                    </el-form-item>

                    <el-form-item label="认证方式">
                        <el-radio-group v-model="item.auth_type">
                            <el-radio label="password">用户名 + 密码</el-radio>
                            <el-radio label="api_key">API Key</el-radio>
                        </el-radio-group>
                        <div class="field-help">
                            qB 5.2 及以上可以在「选项 → Web UI → API Key」里生成；老版本用用户名密码。
                        </div>
                    </el-form-item>

                    <el-form-item v-if="item.auth_type === 'password'" label="用户名">
                        <el-input v-model="item.username" placeholder="qBittorrent 登录用户名" clearable />
                    </el-form-item>

                    <el-form-item v-if="item.auth_type === 'password'" label="密码">
                        <el-input v-model="item.password" type="password" show-password placeholder="qBittorrent 登录密码" />
                        <div class="field-help">留空保存则沿用原来的密码。</div>
                    </el-form-item>

                    <el-form-item v-else class="form-span2" label="API Key">
                        <el-input v-model="item.api_key" placeholder="qBittorrent API Key" clearable />
                        <div class="field-help">留空保存则沿用原来的 Key。</div>
                    </el-form-item>
                    </div>
                </el-form>

                <div
                    v-if="item.testResult"
                    :style="{
                        marginTop: '6px',
                        padding: '10px 12px',
                        borderRadius: '6px',
                        fontSize: '13px',
                        background: item.testOk ? '#f0f9eb' : '#fef0f0',
                        color: item.testOk ? '#529b2e' : '#c45656',
                        border: '1px solid ' + (item.testOk ? '#e1f3d8' : '#fde2e2'),
                    }">
                    {{ item.testOk ? '✓ ' : '✕ ' }}{{ item.testResult }}
                </div>
            </el-card>
        </div>
    `,
});
