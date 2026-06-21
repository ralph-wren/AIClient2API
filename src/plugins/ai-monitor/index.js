import { CONFIG } from '../../core/config-manager.js';
import logger from '../../utils/logger.js';

const DEFAULT_MAX_PAYLOAD_CHARS = 4000;

function getMonitorOptions(config = CONFIG) {
    return {
        logFullPayload: config.AI_MONITOR_LOG_FULL_PAYLOAD === true,
        maxPayloadChars: Number.isFinite(Number(config.AI_MONITOR_LOG_MAX_PAYLOAD_CHARS))
            ? Math.max(0, Number(config.AI_MONITOR_LOG_MAX_PAYLOAD_CHARS))
            : DEFAULT_MAX_PAYLOAD_CHARS
    };
}

function truncateText(value, maxChars) {
    const text = String(value ?? '');
    if (maxChars <= 0 || text.length <= maxChars) {
        return text;
    }

    return `${text.slice(0, maxChars)}...[truncated ${text.length - maxChars} chars]`;
}

function safeStringify(value, maxChars = DEFAULT_MAX_PAYLOAD_CHARS) {
    const seen = new WeakSet();
    let json;

    try {
        json = JSON.stringify(value, (key, currentValue) => {
            if (key === 'thoughtSignature') {
                return '[redacted]';
            }

            if (typeof currentValue === 'string') {
                return truncateText(currentValue, maxChars);
            }

            if (currentValue && typeof currentValue === 'object') {
                if (seen.has(currentValue)) {
                    return '[Circular]';
                }
                seen.add(currentValue);
            }

            return currentValue;
        });
    } catch (error) {
        json = `[unserializable payload: ${error.message}]`;
    }

    return truncateText(json, maxChars);
}

function countText(value) {
    let length = 0;
    const seen = new WeakSet();

    const visit = (item) => {
        if (item == null) {
            return;
        }

        if (typeof item === 'string') {
            length += item.length;
            return;
        }

        if (Array.isArray(item)) {
            item.forEach(visit);
            return;
        }

        if (typeof item === 'object') {
            if (seen.has(item)) {
                return;
            }
            seen.add(item);

            for (const [key, child] of Object.entries(item)) {
                if (key === 'thoughtSignature') {
                    continue;
                }

                if (child && (typeof child === 'object' || typeof child === 'string')) {
                    visit(child);
                }
            }
        }
    };

    visit(value);
    return length;
}

function extractUsage(value) {
    const usageList = [];
    const seen = new WeakSet();

    const visit = (item) => {
        if (!item || typeof item !== 'object') {
            return;
        }

        if (seen.has(item)) {
            return;
        }
        seen.add(item);

        if (item.usage) {
            usageList.push(item.usage);
        }
        if (item.usageMetadata) {
            usageList.push(item.usageMetadata);
        }

        if (Array.isArray(item)) {
            item.forEach(visit);
            return;
        }

        for (const child of Object.values(item)) {
            if (child && typeof child === 'object') {
                visit(child);
            }
        }
    };

    visit(value);
    return usageList.at(-1) || null;
}

function getRequestSummary(body) {
    if (!body || typeof body !== 'object') {
        return `type=${typeof body}`;
    }

    const messageCount = Array.isArray(body.messages) ? body.messages.length : 0;
    const contentCount = Array.isArray(body.contents) ? body.contents.length : 0;
    const inputCount = Array.isArray(body.input) ? body.input.length : 0;
    const textLength = countText(body);

    return `messages=${messageCount}, contents=${contentCount}, input=${inputCount}, textChars=${textLength}`;
}

function getResponseSummary(chunks) {
    const chunkCount = Array.isArray(chunks) ? chunks.length : (chunks == null ? 0 : 1);
    const textLength = countText(chunks);
    const usage = extractUsage(chunks);
    const usageSummary = usage ? `, usage=${safeStringify(usage, 1000)}` : '';

    return `chunks=${chunkCount}, textChars=${textLength}${usageSummary}`;
}

function logFullPayload(label, value, options) {
    if (!options.logFullPayload) {
        return;
    }

    logger.debug(`${label}: ${safeStringify(value, options.maxPayloadChars)}`);
}

/**
 * AI 接口监控插件
 * 功能：
 * 1. 捕获 AI 接口的请求参数（转换前和转换后）
 * 2. 捕获 AI 接口的响应结果（转换前和转换后，流式响应聚合输出）
 */
const aiMonitorPlugin = {
    name: 'ai-monitor',
    version: '1.0.0',
    description: 'AI 接口监控插件 - 捕获请求和响应参数（全链路协议转换监控，流式聚合输出，用于调试和分析）',
    type: 'middleware',
    _priority: 100,

    // 用于存储流式响应的中间状态
    streamCache: new Map(),

    async init(config) {
        logger.info(`[AI Monitor Plugin] Initialized | fullPayload=${getMonitorOptions(config).logFullPayload}`);
    },

    /**
     * 中间件：初始化请求上下文
     */
    async middleware(req, res, requestUrl, config) {
        const aiPaths = [
            '/v1/chat/completions', 
            '/v1/responses', 
            '/v1/messages', 
            '/v1beta/models',
            '/v1/images/generations',
            '/v1/images/edits'
        ];
        const isAiPath = aiPaths.some(path => requestUrl.pathname.includes(path));

        if (isAiPath && req.method === 'POST' && !config._monitorRequestId) {
            // 在监控插件中生成请求标识，并存入 config 以供全链路追踪
            const requestId = Date.now() + Math.random().toString(36).substring(2, 10);
            config._monitorRequestId = requestId;
        }
        
        return { handled: false };
    },

    hooks: {
        /**
         * 请求转换后的钩子
         */
        async onContentGenerated(config) {
            const { originalRequestBody, processedRequestBody, fromProvider, toProvider, model, _monitorRequestId, isStream } = config;
            if (!originalRequestBody) return;
            const traceRequestId = _monitorRequestId;
            const monitorOptions = getMonitorOptions(config);

            setImmediate(() => {
                const hasConversion = fromProvider !== toProvider;
                logger.info(`[AI Monitor][${traceRequestId}] >>> Req Protocol: ${fromProvider}${hasConversion ? ' -> ' + toProvider : ''} | Model: ${model} | ${getRequestSummary(originalRequestBody)}`);
                
                if (hasConversion) {
                    logFullPayload(`[AI Monitor][${traceRequestId}] [Req Original]`, originalRequestBody, monitorOptions);
                    logFullPayload(`[AI Monitor][${traceRequestId}] [Req Processed]`, processedRequestBody, monitorOptions);
                } else {
                    logFullPayload(`[AI Monitor][${traceRequestId}] [Req]`, originalRequestBody, monitorOptions);
                }
            });

            // 处理流式响应的聚合输出
            if (isStream && traceRequestId) {
                setTimeout(() => {
                    const cache = aiMonitorPlugin.streamCache.get(traceRequestId);
                    if (cache) {
                        const hasConversion = cache.fromProvider !== cache.toProvider;
                        const conversionPrefix = hasConversion ? `${cache.toProvider} -> ` : '';
                        const convertedSummary = hasConversion ? ` | converted(${getResponseSummary(cache.convertedChunks)})` : '';
                        logger.info(`[AI Monitor][${traceRequestId}] <<< Stream Response Aggregated: ${conversionPrefix}${cache.fromProvider} | native(${getResponseSummary(cache.nativeChunks)})${convertedSummary}`);
                        
                        if (hasConversion) {
                            logFullPayload(`[AI Monitor][${traceRequestId}] [Res Native Full]`, cache.nativeChunks, monitorOptions);
                            logFullPayload(`[AI Monitor][${traceRequestId}] [Res Converted Full]`, cache.convertedChunks, monitorOptions);
                        } else {
                            logFullPayload(`[AI Monitor][${traceRequestId}] [Res Full]`, cache.nativeChunks, monitorOptions);
                        }
                        
                        aiMonitorPlugin.streamCache.delete(traceRequestId);
                    }
                }, 2000); // 等待流传输完成
            }
        },

        /**
         * 非流式响应转换监控
         */
        async onUnaryResponse({ nativeResponse, clientResponse, fromProvider, toProvider, requestId }) {
            const monitorOptions = getMonitorOptions(CONFIG);

            setImmediate(() => {
                const reqId = requestId || 'N/A';
                const hasConversion = fromProvider !== toProvider;
                const conversionPrefix = hasConversion ? `${toProvider} -> ` : '';
                const convertedSummary = hasConversion ? ` | converted(${getResponseSummary(clientResponse)})` : '';
                logger.info(`[AI Monitor][${reqId}] <<< Res Protocol: ${conversionPrefix}${fromProvider} (Unary) | native(${getResponseSummary(nativeResponse)})${convertedSummary}`);
                
                if (hasConversion) {
                    logFullPayload(`[AI Monitor][${reqId}] [Res Native]`, nativeResponse, monitorOptions);
                    logFullPayload(`[AI Monitor][${reqId}] [Res Converted]`, clientResponse, monitorOptions);
                } else {
                    logFullPayload(`[AI Monitor][${reqId}] [Res]`, nativeResponse, monitorOptions);
                }
            });
        },

        /**
         * 流式响应分块转换监控 - 聚合数据
         */
        async onStreamChunk({ nativeChunk, chunkToSend, fromProvider, toProvider, requestId }) {
            if (!requestId) return;

            if (!aiMonitorPlugin.streamCache.has(requestId)) {
                aiMonitorPlugin.streamCache.set(requestId, {
                    nativeChunks: [],
                    convertedChunks: [],
                    fromProvider,
                    toProvider
                });
            }

            const cache = aiMonitorPlugin.streamCache.get(requestId);
            
            // 过滤 null 值，并判断是否为数组类型
            if (nativeChunk != null) {
                if (Array.isArray(nativeChunk)) {
                    cache.nativeChunks.push(...nativeChunk.filter(item => item != null));
                } else {
                    cache.nativeChunks.push(nativeChunk);
                }
            }
            
            if (chunkToSend != null) {
                if (Array.isArray(chunkToSend)) {
                    cache.convertedChunks.push(...chunkToSend.filter(item => item != null));
                } else {
                    cache.convertedChunks.push(chunkToSend);
                }
            }
        },

        /**
         * 内部请求转换监控
         */
        async onInternalRequestConverted({ requestId, internalRequest, converterName }) {
            const monitorOptions = getMonitorOptions(CONFIG);

            setImmediate(() => {
                const reqId = requestId || 'N/A';
                logger.info(`[AI Monitor][${reqId}] >>> Internal Req Converted [${converterName}] | ${getRequestSummary(internalRequest)}`);
                logFullPayload(`[AI Monitor][${reqId}] [Internal Req]`, internalRequest, monitorOptions);
            });
        }
    }
};

export default aiMonitorPlugin;
