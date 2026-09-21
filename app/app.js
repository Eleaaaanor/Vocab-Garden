/* ==========================================================================
 * GRE Vocabulary Garden — 运行时逻辑（无框架、无网络、无持久化）
 *
 * 数据只来自构建时注入的 VOCABULARY_DATA / LIST_DATA（见 tools/build.py）。
 * 本文件不写入 localStorage / IndexedDB / cookie，也不发起任何网络请求；
 * 刷新页面后 session 状态自然归零。
 * ========================================================================== */
(function () {
  'use strict';

  /* ----------------------------------------------------------- utilities */

  var HTML_ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(
      /[&<>"']/g,
      function (ch) { return HTML_ENTITIES[ch]; }
    );
  }

  function toStr(value) {
    if (typeof value === 'string') return value.trim();
    if (value === null || value === undefined) return '';
    return String(value).trim();
  }

  function toStrList(value) {
    if (!Array.isArray(value)) return [];
    var out = [];
    for (var i = 0; i < value.length; i += 1) {
      var item = toStr(value[i]);
      if (item) out.push(item);
    }
    return out;
  }

  function toObjList(value) {
    if (!Array.isArray(value)) return [];
    var out = [];
    for (var i = 0; i < value.length; i += 1) {
      if (value[i] && typeof value[i] === 'object') out.push(value[i]);
    }
    return out;
  }

  function normKey(value) {
    return String(value || '').trim().toLowerCase().replace(/\s+/g, ' ');
  }

  function mulberry32(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function shuffled(values, rng) {
    var out = values.slice();
    for (var i = out.length - 1; i > 0; i -= 1) {
      var j = Math.floor(rng() * (i + 1));
      var tmp = out[i];
      out[i] = out[j];
      out[j] = tmp;
    }
    return out;
  }

  function byTerm(a, b) {
    return String(a).toLowerCase().localeCompare(String(b).toLowerCase());
  }

  /* --------------------------------------------------------------- data  */

  function normalizeWord(raw, index) {
    try {
      if (!raw || typeof raw !== 'object') return null;
      var term = toStr(raw.term);
      if (!term) return null;
      var word = {
        id: toStr(raw.id) || 'word-' + index,
        term: term,
        lemma: toStr(raw.lemma) || term,
        pos: toStrList(raw.pos),
        chinese: toStrList(raw.chinese),
        definitions: toObjList(raw.english_definitions)
          .map(function (item) {
            return { pos: toStr(item.pos), definition: toStr(item.definition) };
          })
          .filter(function (item) { return item.definition; }),
        synonyms: toStrList(raw.synonyms),
        examples: toObjList(raw.examples)
          .map(function (item) {
            return {
              sentence: toStr(item.sentence),
              translation: toStr(item.translation),
              source_type: toStr(item.source_type),
              author: toStr(item.author),
              url: toStr(item.url)
            };
          })
          .filter(function (item) { return item.sentence; }),
        derivatives: toObjList(raw.derivatives)
          .map(function (item) {
            return { term: toStr(item.term), relation: toStr(item.relation) };
          })
          .filter(function (item) { return item.term; }),
        confusables: toObjList(raw.confusables)
          .map(function (item) {
            return { term: toStr(item.term), distinction: toStr(item.distinction) };
          })
          .filter(function (item) { return item.term; }),
        notes: toStrList(raw.personal_notes),
        flags: toStrList(raw.review_flags)
      };
      word.blob = [word.term, word.lemma]
        .concat(word.synonyms)
        .concat(word.chinese)
        .join(' ')
        .toLowerCase();
      return word;
    } catch (error) {
      // 单条脏数据不影响整个页面
      return null;
    }
  }

  var RAW_WORDS = null;
  var RAW_LISTS = null;
  try {
    RAW_WORDS = typeof VOCABULARY_DATA !== 'undefined' && VOCABULARY_DATA ? VOCABULARY_DATA.words : null;
  } catch (error) { RAW_WORDS = null; }
  try {
    RAW_LISTS = typeof LIST_DATA !== 'undefined' && LIST_DATA ? LIST_DATA.lists : null;
  } catch (error) { RAW_LISTS = null; }

  var WORDS = [];
  (Array.isArray(RAW_WORDS) ? RAW_WORDS : []).forEach(function (raw, index) {
    var word = normalizeWord(raw, index);
    if (word) WORDS.push(word);
  });

  var wordById = new Map();
  var wordByKey = new Map();
  WORDS.forEach(function (word) {
    if (!wordById.has(word.id)) wordById.set(word.id, word);
    [word.term, word.lemma].forEach(function (value) {
      var key = normKey(value);
      if (key && !wordByKey.has(key)) wordByKey.set(key, word);
    });
  });

  var LISTS = (Array.isArray(RAW_LISTS) ? RAW_LISTS : []).map(function (raw, index) {
    var item = raw && typeof raw === 'object' ? raw : {};
    var ids = toStrList(item.word_ids).filter(function (id) { return wordById.has(id); });
    ids = ids.filter(function (id, position) { return ids.indexOf(id) === position; });
    return {
      id: toStr(item.id) || 'list-' + index,
      name: toStr(item.name) || toStr(item.id) || 'List ' + (index + 1),
      description: toStr(item.description),
      word_ids: ids
    };
  });
  if (!LISTS.some(function (list) { return list.id === 'all'; })) {
    LISTS.unshift({
      id: 'all',
      name: 'All Words',
      description: '',
      word_ids: WORDS.map(function (word) { return word.id; })
    });
  }

  function findList(listId) {
    for (var i = 0; i < LISTS.length; i += 1) {
      if (LISTS[i].id === listId) return LISTS[i];
    }
    return LISTS[0] || null;
  }

  /* ------------------------------------------------------- synonym graph */

  var EDGES = [];
  var adjacency = new Map(); // id -> Map(otherId -> [kinds])
  var edgeIndex = new Map();

  function addEdge(a, b, kind) {
    if (!a || !b || a === b) return;
    var key = a < b ? a + '|' + b : b + '|' + a;
    var existing = edgeIndex.get(key);
    if (existing) {
      if (existing.kinds.indexOf(kind) === -1) existing.kinds.push(kind);
    } else {
      existing = { a: a < b ? a : b, b: a < b ? b : a, kinds: [kind] };
      edgeIndex.set(key, existing);
      EDGES.push(existing);
    }
    [a, b].forEach(function (from) {
      var other = from === a ? b : a;
      if (!adjacency.has(from)) adjacency.set(from, new Map());
      var bucket = adjacency.get(from);
      if (!bucket.has(other)) bucket.set(other, []);
      var kinds = bucket.get(other);
      if (kinds.indexOf(kind) === -1) kinds.push(kind);
    });
  }

  function linkTo(term, owner, kind) {
    var target = wordByKey.get(normKey(term));
    if (!target || target.id === owner.id) return;
    addEdge(owner.id, target.id, kind);
  }

  WORDS.forEach(function (word) {
    word.synonyms.forEach(function (term) { linkTo(term, word, 'synonym'); });
    word.derivatives.forEach(function (item) { linkTo(item.term, word, 'derivative'); });
    word.confusables.forEach(function (item) { linkTo(item.term, word, 'confusable'); });
  });

  function neighborsOf(wordId) {
    var bucket = adjacency.get(wordId);
    if (!bucket) return [];
    var out = [];
    bucket.forEach(function (kinds, id) {
      var word = wordById.get(id);
      if (!word) return;
      out.push({ id: id, word: word, kinds: kinds.slice(), rank: kinds.indexOf('synonym') === -1 ? 1 : 0 });
    });
    out.sort(function (a, b) {
      if (a.rank !== b.rank) return a.rank - b.rank;
      return byTerm(a.word.term, b.word.term);
    });
    return out;
  }

  function degreeOf(wordId) {
    var bucket = adjacency.get(wordId);
    return bucket ? bucket.size : 0;
  }

  /* ----------------------------------------------------------------- ui */

  var ui = {
    selectedListId: 'all',
    query: '',
    browseListId: '__all__',
    browseLimit: 150,
    gardenSeed: 1,
    gardenQuery: ''
  };

  var session = createSession();

  /* ---------------------------------------------------- discarded words */

  /* 唯一被持久化的状态：丢弃标记（localStorage）。session 进度、分数、当前 list
     仍然只存在内存里，刷新即归零。存储不可用时自动退化为“仅本次可用”。 */
  var DISCARD_KEY = 'gre-vocab:discarded:v1';
  var discardedIds = loadDiscarded();

  function loadDiscarded() {
    var result = new Set();
    try {
      var raw = window.localStorage.getItem(DISCARD_KEY);
      if (!raw) return result;
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return result;
      parsed.forEach(function (id) {
        if (typeof id === 'string' && wordById.has(id)) result.add(id);
      });
    } catch (error) {
      // 隐私模式 / 存储损坏：当作没有标记，功能仍然可用
    }
    return result;
  }

  function saveDiscarded() {
    try {
      window.localStorage.setItem(DISCARD_KEY, JSON.stringify(discardedIdsArray()));
      return true;
    } catch (error) {
      return false;
    }
  }

  function discardedIdsArray() {
    var ids = [];
    discardedIds.forEach(function (id) { ids.push(id); });
    return ids.sort();
  }

  function isDiscarded(wordId) {
    return discardedIds.has(wordId);
  }

  function discardedWords() {
    var words = [];
    discardedIds.forEach(function (id) {
      var word = wordById.get(id);
      if (word) words.push(word);
    });
    words.sort(function (a, b) { return byTerm(a.term, b.term); });
    return words;
  }

  function toggleDiscard(wordId) {
    var word = wordById.get(wordId);
    if (!word) return;
    var marked = !discardedIds.has(wordId);
    if (marked) discardedIds.add(wordId);
    else discardedIds.delete(wordId);
    var saved = saveDiscarded();
    showToast(
      (marked ? 'Marked as discarded' : 'Discard mark removed') +
        (saved ? '' : ' (this session only — storage unavailable)')
    );
    render();
  }

  function clearDiscarded(skipConfirm) {
    if (!discardedIds.size) return;
    if (!skipConfirm && !window.confirm('Clear all discard marks? This only affects this browser.')) return;
    discardedIds = new Set();
    saveDiscarded();
    showToast('Discard marks cleared');
    render();
  }

  /* ------------------------------------------- score of the last session */

  /* 第二项持久化数据：最近一次“完整完成”的 session 的最终 score。
     中途退出不写入；新 session 永远从 0 开始，不读取这里的旧值。
     没有记录 = 未练习（UNPRACTICED = -1）。 */
  var SCORE_KEY = 'gre-vocab:scores:v1';
  var UNPRACTICED = -1;
  var storedScores = loadScores();

  function loadScores() {
    var result = Object.create(null);
    try {
      var raw = window.localStorage.getItem(SCORE_KEY);
      if (!raw) return result;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return result;
      Object.keys(parsed).forEach(function (id) {
        var value = parsed[id];
        if (typeof value !== 'number' || !isFinite(value)) return;
        if (!wordById.has(id)) return; // 词库里已经没有这个 id
        if (value < 0) return; // -1 = 未练习，等同于没有记录
        result[id] = Math.max(0, Math.min(10, Math.round(value)));
      });
    } catch (error) {
      // 存储不可用 / 数据损坏：全部按未练习处理，页面照常工作
    }
    return result;
  }

  function saveScores(scores) {
    try {
      window.localStorage.setItem(SCORE_KEY, JSON.stringify(scores));
      return true;
    } catch (error) {
      return false;
    }
  }

  function scoreOf(wordId) {
    return Object.prototype.hasOwnProperty.call(storedScores, wordId)
      ? storedScores[wordId]
      : UNPRACTICED;
  }

  function practicedScores() {
    var out = [];
    WORDS.forEach(function (word) {
      var score = scoreOf(word.id);
      if (score >= 0) out.push({ word: word, score: score });
    });
    return out;
  }

  /* 5 档：0 / 1-3 / 4-6 / 7-9 / 10（未练习不进入统计） */
  var SCORE_BUCKETS = [
    { id: 'zero', label: '0', min: 0, max: 0 },
    { id: 'low', label: '1-3', min: 1, max: 3 },
    { id: 'mid', label: '4-6', min: 4, max: 6 },
    { id: 'high', label: '7-9', min: 7, max: 9 },
    { id: 'top', label: '10', min: 10, max: 10 }
  ];

  function scoreStats() {
    var buckets = SCORE_BUCKETS.map(function (bucket) {
      return {
        id: bucket.id,
        label: bucket.label,
        min: bucket.min,
        max: bucket.max,
        words: []
      };
    });
    var unpracticed = 0;
    WORDS.forEach(function (word) {
      var score = scoreOf(word.id);
      if (score < 0) {
        unpracticed += 1;
        return;
      }
      for (var i = 0; i < buckets.length; i += 1) {
        if (score >= buckets[i].min && score <= buckets[i].max) {
          buckets[i].words.push(word);
          return;
        }
      }
    });
    var practiced = 0;
    buckets.forEach(function (bucket) {
      bucket.words.sort(function (a, b) { return byTerm(a.term, b.term); });
      practiced += bucket.words.length;
    });
    return { buckets: buckets, practiced: practiced, unpracticed: unpracticed, total: WORDS.length };
  }

  function persistSessionScores(result) {
    if (!result || !result.scores) return;
    Object.keys(result.scores).forEach(function (id) {
      storedScores[id] = Math.max(0, Math.min(10, result.scores[id]));
    });
    if (!saveScores(storedScores)) {
      showToast('Scores could not be saved (storage unavailable)');
    }
  }

  function discardToggleMarkup(wordId, compact) {
    var marked = isDiscarded(wordId);
    return (
      '<button class="btn btn-sm btn-ghost discard-toggle' + (marked ? ' is-on' : '') + '"' +
        ' data-act="toggle-discard" data-word-id="' + esc(wordId) + '"' +
        ' aria-pressed="' + (marked ? 'true' : 'false') + '"' +
        ' title="' + (marked ? 'Unmark this word' : 'Mark this word as discarded') + '">' +
        (marked ? (compact ? '⊘' : '⊘ Discarded') : (compact ? '⊘' : '⊘ Discard')) +
      '</button>'
    );
  }

  function createSession() {
    return {
      active: false,
      listId: '',
      listName: '',
      order: [],
      queue: [],
      pos: 0,
      round: 1,
      roundsCompleted: 0,
      /* 卡片正面 / 反面（点击卡片切换），以及反面里中文释义的显示状态。
         两者都只属于当前展示，不写 localStorage。 */
      cardFace: 'front',
      chineseShown: false,
      status: Object.create(null),
      /* 已提交的本次 session 累计 score（上限 10）：每一轮结束时结算一次，
         同一个词在同一轮内反复改评分不会重复累计。 */
      score: Object.create(null),
      roundWorst: Object.create(null),
      result: null
    };
  }

  function currentWord() {
    if (!session.active) return null;
    var id = session.queue[session.pos];
    return id ? wordById.get(id) || null : null;
  }

  /* -------------------------------------------------------------- router */

  function currentRoute() {
    var raw = String(location.hash || '').replace(/^#\/?/, '');
    var parts = raw.split('/').filter(Boolean);
    var name = 'garden';
    var param = '';
    try {
      name = parts.length ? decodeURIComponent(parts[0]) : 'garden';
      param = parts.length > 1 ? decodeURIComponent(parts.slice(1).join('/')) : '';
    } catch (error) {
      name = 'garden';
      param = '';
    }
    if (['garden', 'browse', 'lists', 'export', 'word', 'session', 'result'].indexOf(name) === -1) {
      return { name: 'garden', param: '' };
    }
    return { name: name, param: param };
  }

  function go(hash) {
    if (location.hash === hash) render();
    else location.hash = hash;
  }

  function openWord(wordId) {
    if (!wordId || !wordById.has(wordId)) {
      showToast('Word not found');
      return;
    }
    go('#/word/' + encodeURIComponent(wordId));
  }

  /* --------------------------------------------------------- view shell */

  var topbarEl = null;
  var viewEl = null;
  var toastEl = null;
  var toastTimer = null;

  function renderTopbar(route) {
    if (!topbarEl) return;
    if (route.name === 'session') {
      topbarEl.innerHTML = '<div class="brand"><b>Vocabulary Garden</b></div>';
      return;
    }
    var tabs = [
      { name: 'garden', label: 'Garden', hash: '#/' },
      { name: 'browse', label: 'Bank', hash: '#/browse' },
      { name: 'lists', label: 'Lists', hash: '#/lists' },
      { name: 'export', label: 'Export', hash: '#/export' }
    ];
    var current = route.name === 'word' || route.name === 'result' ? '' : route.name;
    topbarEl.innerHTML =
      '<div class="brand"><b>Vocabulary Garden</b></div>' +
      '<nav class="tabs">' +
      tabs
        .map(function (tab) {
          var isCurrent = tab.name === current ? ' aria-current="page"' : '';
          return '<a class="tab" href="' + tab.hash + '"' + isCurrent + '>' + esc(tab.label) + '</a>';
        })
        .join('') +
      '</nav>';
  }

  function showToast(message) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.classList.add('show');
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastEl.classList.remove('show');
    }, 2200);
  }

  function errorPanel(error) {
    return (
      '<div class="card"><h1 class="term">Something went wrong</h1>' +
      '<p class="subtle">This view failed to render. The rest of the app still works.</p>' +
      '<p class="subtle">' + esc(error && error.message ? error.message : String(error)) + '</p>' +
      '<p><a class="btn" href="#/">Back to Garden</a></p></div>'
    );
  }

  function render() {
    if (!viewEl) return;
    var route = currentRoute();

    if (route.name === 'session' && !session.active) {
      location.replace('#/lists');
      return;
    }
    if (route.name === 'result' && !session.result) {
      location.replace('#/');
      return;
    }

    renderTopbar(route);
    var routeKey = route.name + '|' + route.param;
    /* 重新赋值 innerHTML 会让文档高度瞬间归零，浏览器会把滚动位置夹到顶部；
       同页面重绘（例如在 Lists 页选择 list）时必须自己把滚动位置恢复回来。 */
    var resetScroll = routeKey !== lastRouteKey || route.name === 'session';
    var keepScroll = resetScroll ? null : window.scrollY;
    lastRouteKey = routeKey;

    try {
      var html = '';
      if (route.name === 'browse') html = renderBrowse();
      else if (route.name === 'lists') html = renderLists();
      else if (route.name === 'export') html = renderExport();
      else if (route.name === 'word') html = renderWord(route.param);
      else if (route.name === 'session') html = renderSession();
      else if (route.name === 'result') html = renderResult();
      else html = renderGarden();
      viewEl.innerHTML = html;
    } catch (error) {
      viewEl.innerHTML = errorPanel(error);
    }

    if (resetScroll) {
      window.scrollTo(0, 0);
    } else if (keepScroll !== null && Math.abs(window.scrollY - keepScroll) > 1) {
      window.scrollTo(0, keepScroll);
    }

    window.requestAnimationFrame(function () {
      fillDeferredGraphs();
      if (!resetScroll && keepScroll !== null && Math.abs(window.scrollY - keepScroll) > 1) {
        window.scrollTo(0, keepScroll);
      }
    });
  }

  var lastRouteKey = '';

  /* ---------------------------------------------------------- garden view */

  var gardenCache = { seed: null, limit: 0, ids: null };
  var gardenUsed = { width: 0, height: 0 };
  var localUsed = { width: 0, height: 0 };

  /* 图形按真实 CSS 像素布局：viewBox 与容器同尺寸，因此字号/圆点在任何屏幕上
     都保持同样大小，手机上不会缩成看不见的点。 */
  function measureBox(element, minWidth, minHeight) {
    if (!element) return null;
    var rect = element.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return null;
    return {
      width: Math.max(minWidth, Math.round(rect.width)),
      height: Math.max(minHeight, Math.round(rect.height))
    };
  }

  function sampleLimit(size) {
    var density = (size.width * size.height) / 5400;
    return Math.max(12, Math.min(56, Math.round(density)));
  }

  function gardenSample(seed, limit) {
    var rng = mulberry32(seed * 7919 + 13);
    var connected = [];
    var allIds = [];
    WORDS.forEach(function (word) {
      allIds.push(word.id);
      if (degreeOf(word.id) > 0) connected.push(word.id);
    });
    connected.sort();
    allIds.sort();

    var picked = [];
    var pickedSet = new Set();
    function add(id) {
      if (!id || pickedSet.has(id) || picked.length >= limit) return;
      if (!wordById.has(id)) return;
      pickedSet.add(id);
      picked.push(id);
    }

    // 先随机挑几个词，再把它们的同义 / 派生 / 易混邻居一起放进来，图上因此一定有连线；
    // 剩下的位置随机补齐（包括没有任何关系的词，它们依然会显示成单独的圆点）。
    var neighborhoodBudget = Math.round(limit * 0.72);
    var seeds = shuffled(connected, rng);
    for (var i = 0; i < seeds.length && picked.length < neighborhoodBudget; i += 1) {
      if (pickedSet.has(seeds[i])) continue;
      add(seeds[i]);
      neighborsOf(seeds[i]).forEach(function (item) { add(item.id); });
    }

    shuffled(allIds, rng).forEach(add);
    return picked;
  }

  function layoutGraph(ids, seed, size) {
    var W = size.width;
    var H = size.height;
    var CX = W / 2;
    var CY = H / 2;
    var unit = Math.max(0.62, Math.min(1.15, Math.min(W, H * 1.3) / 700));
    var idSet = new Set(ids);
    var rng = mulberry32((seed + 1) * 104729);

    var localAdj = new Map();
    ids.forEach(function (id) { localAdj.set(id, []); });
    EDGES.forEach(function (edge) {
      if (!idSet.has(edge.a) || !idSet.has(edge.b)) return;
      localAdj.get(edge.a).push({ id: edge.b, kinds: edge.kinds });
      localAdj.get(edge.b).push({ id: edge.a, kinds: edge.kinds });
    });

    var seen = new Set();
    var clusters = [];
    var singles = [];
    ids.forEach(function (id) {
      if (seen.has(id)) return;
      var stack = [id];
      var comp = [];
      seen.add(id);
      while (stack.length) {
        var cur = stack.pop();
        comp.push(cur);
        localAdj.get(cur).forEach(function (nb) {
          if (!seen.has(nb.id)) {
            seen.add(nb.id);
            stack.push(nb.id);
          }
        });
      }
      if (comp.length > 1) clusters.push(comp);
      else singles.push(comp[0]);
    });

    clusters.sort(function (a, b) {
      if (b.length !== a.length) return b.length - a.length;
      return byTerm(a[0], b[0]);
    });
    singles.sort(byTerm);

    var positions = new Map();
    var GOLDEN = Math.PI * (3 - Math.sqrt(5));

    clusters.forEach(function (comp, index) {
      var angle = index * GOLDEN - Math.PI / 2;
      var orbit = 60 + 56 * Math.sqrt(index) * unit;
      var cx = CX + Math.cos(angle) * orbit * 1.08;
      var cy = CY + Math.sin(angle) * orbit * 0.88;
      var ring = Math.max(34, 6.2 * comp.length) * unit;
      var ordered = comp.slice().sort(byTerm);
      var offset = rng() * Math.PI * 2;
      ordered.forEach(function (id, position) {
        var a = offset + (position / ordered.length) * Math.PI * 2;
        positions.set(id, {
          x: cx + Math.cos(a) * ring + (rng() - 0.5) * 5,
          y: cy + Math.sin(a) * ring + (rng() - 0.5) * 5,
          labDir: { x: Math.cos(a), y: Math.sin(a) }
        });
      });
    });

    singles.forEach(function (id, index) {
      var a = (index / Math.max(1, singles.length)) * Math.PI * 2 + 0.5;
      var wobble = 0.86 + (index % 3) * 0.05;
      positions.set(id, {
        x: CX + Math.cos(a) * W * 0.42 * wobble + (rng() - 0.5) * 8,
        y: CY + Math.sin(a) * H * 0.4 * wobble + (rng() - 0.5) * 8,
        labDir: { x: 0, y: index % 2 ? -1 : 1 }
      });
    });

    normalizePositions(positions, W, H, 30);

    var radiusOf = new Map();
    positions.forEach(function (point, id) {
      var deg = localAdj.get(id).length;
      radiusOf.set(id, Math.max(5.5, Math.min(15, 5 + 2.6 * Math.sqrt(deg))));
    });

    var links = [];
    EDGES.forEach(function (edge) {
      if (!idSet.has(edge.a) || !idSet.has(edge.b)) return;
      edge.kinds.forEach(function (kind) {
        links.push({ a: edge.a, b: edge.b, kind: kind });
      });
    });

    return {
      width: W,
      height: H,
      positions: positions,
      radiusOf: radiusOf,
      links: links,
      localDegree: function (id) { return localAdj.get(id) ? localAdj.get(id).length : 0; }
    };
  }

  function normalizePositions(positions, width, height, pad) {
    if (!positions.size) return;
    var minX = Infinity;
    var maxX = -Infinity;
    var minY = Infinity;
    var maxY = -Infinity;
    positions.forEach(function (point) {
      minX = Math.min(minX, point.x);
      maxX = Math.max(maxX, point.x);
      minY = Math.min(minY, point.y);
      maxY = Math.max(maxY, point.y);
    });
    var spanX = Math.max(1, maxX - minX);
    var spanY = Math.max(1, maxY - minY);
    var scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY, 1.25);
    var offsetX = (width - spanX * scale) / 2 - minX * scale;
    var offsetY = (height - spanY * scale) / 2 - minY * scale;
    positions.forEach(function (point) {
      point.x = point.x * scale + offsetX;
      point.y = point.y * scale + offsetY;
    });
  }

  function graphSvgMarkup(layout, options) {
    var opts = options || {};
    var highlight = opts.highlight || null; // Set of ids or null
    var labelAll = !!opts.labelAll;
    var svg = ['<svg viewBox="0 0 ' + layout.width + ' ' + layout.height + '" role="img" aria-label="Word relation graph">'];

    layout.links.forEach(function (link) {
      var a = layout.positions.get(link.a);
      var b = layout.positions.get(link.b);
      if (!a || !b) return;
      var dim = highlight ? !(highlight.has(link.a) && highlight.has(link.b)) : false;
      svg.push(
        '<line class="g-link g-' + link.kind + (dim ? ' dim' : '') +
          '" x1="' + a.x.toFixed(1) + '" y1="' + a.y.toFixed(1) +
          '" x2="' + b.x.toFixed(1) + '" y2="' + b.y.toFixed(1) + '"></line>'
      );
    });

    var total = layout.positions.size;
    layout.positions.forEach(function (point, id) {
      var word = wordById.get(id);
      if (!word) return;
      var radius = layout.radiusOf.get(id) || 6;
      var deg = layout.localDegree(id);
      var isHit = highlight ? highlight.has(id) : false;
      var classes = ['g-node'];
      if (!deg) classes.push('g-isolated');
      if (isHit) classes.push('hit');
      else if (highlight) classes.push('dim');
      if (opts.centerId === id) classes.push('is-center');
      var showLabel = labelAll || deg >= 2 || total <= 24 || isHit;
      var dir = point.labDir || { x: 0, y: deg % 2 === 0 ? 1 : -1 };
      var gap = radius + 10;
      var labelX = point.x + dir.x * gap;
      var labelY = point.y + dir.y * gap + 3.5;
      var anchor = dir.x > 0.35 ? 'start' : (dir.x < -0.35 ? 'end' : 'middle');
      var tip = word.term + (word.chinese.length ? ' — ' + word.chinese[0] : '');
      svg.push(
        '<g class="' + classes.join(' ') + '" data-word-id="' + esc(id) + '" tabindex="0" role="button" aria-label="' + esc(word.term) + '">' +
          '<title>' + esc(tip) + '</title>' +
          '<circle class="g-hit" cx="' + point.x.toFixed(1) + '" cy="' + point.y.toFixed(1) + '" r="' +
            Math.max(radius + 9, 16).toFixed(1) + '"></circle>' +
          '<circle class="g-ball" cx="' + point.x.toFixed(1) + '" cy="' + point.y.toFixed(1) + '" r="' + radius.toFixed(1) + '"></circle>' +
          (showLabel
            ? '<text x="' + labelX.toFixed(1) + '" y="' + labelY.toFixed(1) + '" text-anchor="' + anchor + '">' +
              esc(word.term) + '</text>'
            : '') +
        '</g>'
      );
    });

    svg.push('</svg>');
    return svg.join('');
  }

  function renderGarden() {
    var list = findList(ui.selectedListId) || findList('all');

    return (
      '<section class="hero">' +
        '<p class="lead">' +
          esc(String(WORDS.length)) + ' words in your bank · ' + esc(String(LISTS.length)) + ' lists' +
        '</p>' +
        '<div class="hero-actions">' +
          '<button class="btn btn-primary" data-act="start-default">Start session</button>' +
          '<a class="btn" href="#/browse">Bank</a>' +
          '<a class="btn btn-ghost" href="#/lists">Lists</a>' +
        '</div>' +
        '<p class="subtle" style="margin-top:10px">' +
          'Current list: ' + esc(list ? list.name : 'All Words') +
          ' (' + esc(String(list ? list.word_ids.length : 0)) + ' words)' +
        '</p>' +
      '</section>' +
      '<section class="garden">' +
        '<div class="garden-head">' +
          '<input class="garden-search" id="garden-search" type="search" autocomplete="off" ' +
            'placeholder="Search words / synonyms" value="' + esc(ui.gardenQuery) + '">' +
          '<span class="spacer"></span>' +
          '<button class="btn btn-sm btn-ghost" data-act="resample">Shuffle</button>' +
        '</div>' +
        '<div class="garden-canvas" id="garden-canvas"></div>' +
        '<div class="legend">' +
          '<span>Bubble size = links</span><span>Solid = synonym</span>' +
          '<span>Dashed = derivative</span><span>Dotted = confusable</span>' +
        '</div>' +
        '<div id="garden-hits">' + gardenHitsMarkup() + '</div>' +
      '</section>'
    );
  }

  function gardenHighlight() {
    var query = ui.gardenQuery.trim().toLowerCase();
    if (!query) return null;
    var tokens = query.split(/\s+/).filter(Boolean);
    var hits = new Set();
    WORDS.forEach(function (word) {
      var ok = tokens.every(function (token) { return word.blob.indexOf(token) !== -1; });
      if (ok) hits.add(word.id);
    });
    return hits;
  }

  function canvasMarkup(size) {
    var limit = sampleLimit(size);
    if (gardenCache.seed !== ui.gardenSeed || gardenCache.limit !== limit || !gardenCache.ids) {
      gardenCache.seed = ui.gardenSeed;
      gardenCache.limit = limit;
      gardenCache.ids = gardenSample(ui.gardenSeed, limit);
    }
    gardenUsed = { width: size.width, height: size.height };
    return graphSvgMarkup(layoutGraph(gardenCache.ids, ui.gardenSeed, size), {
      highlight: gardenHighlight()
    });
  }

  function gardenHitsMarkup() {
    var hits = gardenHighlight();
    if (!hits) return '';
    var list = [];
    hits.forEach(function (id) { list.push(id); });
    list.sort(function (a, b) { return byTerm(wordById.get(a).term, wordById.get(b).term); });
    if (!list.length) {
      return '<p class="empty">No matches here. Try the Bank tab, or pick another list.</p>';
    }
    var shown = list.slice(0, 30);
    return (
      '<div class="chip-cloud">' +
      shown
        .map(function (id) {
          var word = wordById.get(id);
          return '<button class="chip" data-act="open-word" data-word-id="' + esc(id) + '">' +
            esc(word.term) + '<span class="chip-kind">' + esc(String(degreeOf(id))) + '</span></button>';
        })
        .join('') +
      '</div>' +
      (list.length > shown.length
        ? '<p class="subtle">' + esc(String(list.length - shown.length)) + ' more matches — the Bank tab is better for browsing those.</p>'
        : '')
    );
  }

  function refreshGarden() {
    var canvas = document.getElementById('garden-canvas');
    var size = measureBox(canvas, 240, 170);
    if (canvas && size) canvas.innerHTML = canvasMarkup(size);
    var hits = document.getElementById('garden-hits');
    if (hits) hits.innerHTML = gardenHitsMarkup();
  }

  /* 图形尺寸依赖实际布局，所以渲染后再补一次（只在尺寸变化时重画）。 */
  function fillDeferredGraphs() {
    var canvas = document.getElementById('garden-canvas');
    if (canvas) {
      var size = measureBox(canvas, 240, 170);
      if (size && (size.width !== gardenUsed.width || size.height !== gardenUsed.height || !canvas.firstChild)) {
        canvas.innerHTML = canvasMarkup(size);
      }
    }
    var local = document.getElementById('local-graph');
    if (local) {
      var wordId = local.getAttribute('data-word-id');
      var localSize = measureBox(local, 220, 140);
      if (wordId && localSize &&
          (localSize.width !== localUsed.width || localSize.height !== localUsed.height || !local.firstChild)) {
        localUsed = localSize;
        local.innerHTML = localGraphMarkup(wordId, localSize);
      }
    }
  }

  /* --------------------------------------------------------- browse view */

  function listIdSet(listId) {
    if (!listId || listId === '__all__') return null;
    var list = findList(listId);
    if (!list) return null;
    var set = new Set();
    list.word_ids.forEach(function (id) { set.add(id); });
    return set;
  }

  function browseResults() {
    var set = listIdSet(ui.browseListId);
    var tokens = ui.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    var out = [];
    WORDS.forEach(function (word) {
      if (set && !set.has(word.id)) return;
      for (var i = 0; i < tokens.length; i += 1) {
        if (word.blob.indexOf(tokens[i]) === -1) return;
      }
      out.push(word);
    });
    out.sort(function (a, b) { return byTerm(a.term, b.term); });
    return out;
  }

  function wordRowsMarkup(words) {
    if (!words.length) {
      return '<p class="empty">No matches. Try another keyword, or switch the filter back to “All”.</p>';
    }
    return (
      '<div class="word-list">' +
      words
        .map(function (word) {
          return (
            '<button class="word-row" data-act="open-word" data-word-id="' + esc(word.id) + '">' +
              '<span class="w-term">' + esc(word.term) + '</span>' +
              '<span class="w-pos">' + esc(word.pos.join(' / ')) + '</span>' +
              (word.flags.length ? '<span class="w-flag" title="review flags">⚑</span>' : '') +
              (isDiscarded(word.id) ? '<span class="w-drop" title="marked as discarded">⊘</span>' : '') +
              '<span class="w-cn">' + esc(word.chinese.join('；')) + '</span>' +
            '</button>'
          );
        })
        .join('') +
      '</div>'
    );
  }

  function browseBodyMarkup() {
    var results = browseResults();
    var shown = results.slice(0, ui.browseLimit);
    return (
      wordRowsMarkup(shown) +
      (results.length > shown.length
        ? '<div class="load-more"><button class="btn btn-sm" data-act="load-more">Show more (' +
          esc(String(results.length - shown.length)) + ' left)</button></div>'
        : '')
    );
  }

  function browseCountMarkup() {
    var results = browseResults();
    return esc(String(results.length)) + ' words';
  }

  function refreshBrowse() {
    var listEl = document.getElementById('browse-results');
    if (listEl) listEl.innerHTML = browseBodyMarkup();
    var countEl = document.getElementById('browse-count');
    if (countEl) countEl.textContent = browseCountMarkup();
  }

  function renderBrowse() {
    var options = ['<option value="__all__"' + (ui.browseListId === '__all__' ? ' selected' : '') + '>All words</option>']
      .concat(
        LISTS.map(function (list) {
          return '<option value="' + esc(list.id) + '"' + (ui.browseListId === list.id ? ' selected' : '') + '>' +
            esc(list.name) + '（' + esc(String(list.word_ids.length)) + '）</option>';
        })
      )
      .join('');

    return (
      '<div class="view-head">' +
        '<h1>Bank</h1>' +
        '<span class="spacer"></span>' +
        '<span class="subtle" id="browse-count">' + browseCountMarkup() + '</span>' +
      '</div>' +
      '<div class="search-wrap"><div class="search-row">' +
        '<input id="browse-search" type="search" autocomplete="off" placeholder="Search term / synonyms / Chinese" value="' + esc(ui.query) + '">' +
        '<select id="browse-list" aria-label="Filter by list">' + options + '</select>' +
      '</div></div>' +
      '<div id="browse-results">' + browseBodyMarkup() + '</div>'
    );
  }

  /* ---------------------------------------------------------- lists view */

  function renderLists() {
    var cards = LISTS.map(function (list) {
      var selected = list.id === ui.selectedListId;
      return (
        '<button class="list-card" data-act="select-list" data-list-id="' + esc(list.id) + '" aria-pressed="' + (selected ? 'true' : 'false') + '">' +
          '<span class="list-radio" aria-hidden="true"></span>' +
          '<span class="list-body">' +
            '<span class="list-name">' + esc(list.name) + '</span>' +
            (list.description ? '<span class="list-desc">' + esc(list.description) + '</span>' : '') +
          '</span>' +
          '<span class="list-count">' + esc(String(list.word_ids.length)) + ' words</span>' +
        '</button>'
      );
    }).join('');

    var selectedList = findList(ui.selectedListId) || findList('all');
    var empty = selectedList && selectedList.word_ids.length === 0;

    return (
      '<div class="view-head"><h1>Lists</h1></div>' +
      '<p class="subtle">One list per session. Progress is not saved — reloading resets it ' +
        '(discard marks and last-session scores are kept in this browser).</p>' +
      '<div class="list-stack">' + (cards || '<p class="empty">No lists yet in data/lists.json.</p>') + '</div>' +
      '<div class="sticky-actions">' +
        '<button class="btn btn-primary" data-act="start-selected"' + (empty ? ' disabled' : '') + '>' +
          'Start session · ' + esc(selectedList ? selectedList.name : 'All Words') +
        '</button>' +
        (empty ? '<p class="subtle" style="text-align:center">This list has no words to practice.</p>' : '') +
      '</div>'
    );
  }

  /* 被标记丢弃的词：保持原有 localStorage 机制不变，导出统一放在 Export 页。 */
  function discardedSectionMarkup() {
    var words = discardedWords();
    var shown = words.slice(0, 60);
    return (
      '<section class="panel discarded-panel">' +
        '<h2>Discarded words <span class="subtle">(' + esc(String(words.length)) + ')</span></h2>' +
        (words.length
          ? '<p class="subtle">Marked with ⊘ while practising. Kept in this browser only ' +
              '(localStorage) — nothing is removed from data/words.json, and practice is not skipped.</p>' +
            '<div class="chip-cloud">' +
              shown
                .map(function (word) {
                  return '<button class="chip" data-act="open-word" data-word-id="' + esc(word.id) + '">' +
                    esc(word.term) + '<span class="chip-kind">⊘</span></button>';
                })
                .join('') +
            '</div>' +
            (words.length > shown.length
              ? '<p class="subtle">…and ' + esc(String(words.length - shown.length)) + ' more.</p>'
              : '') +
            '<div class="btn-row" style="margin-top:12px">' +
              '<button class="btn btn-sm" data-act="copy-discarded">Copy discarded words</button>' +
              '<button class="btn btn-sm btn-ghost" data-act="clear-discarded">Clear marks</button>' +
            '</div>' +
            '<p class="subtle" style="margin-top:8px">To delete them from the bank: ' +
              '<code>python tools\\remove_words.py &lt;words&gt; --apply</code></p>'
          : '<p class="empty">Nothing discarded yet. Use “⊘ Discard” in a session or on a word card.</p>') +
      '</section>'
    );
  }

  /* ---------------------------------------------------------- export view */

  function donutMarkup(stats) {
    var size = 190;
    var stroke = 18;
    var radius = (size - stroke) / 2;
    var center = size / 2;
    var circumference = 2 * Math.PI * radius;
    var segments = [];
    var offset = 0;

    stats.buckets.forEach(function (bucket) {
      var count = bucket.words.length;
      if (!count || !stats.practiced) return;
      var length = (count / stats.practiced) * circumference;
      segments.push(
        '<circle class="donut-seg donut-' + bucket.id + '" cx="' + center + '" cy="' + center + '"' +
          ' r="' + radius + '" stroke-width="' + stroke + '" fill="none"' +
          ' stroke-dasharray="' + length.toFixed(2) + ' ' + (circumference - length).toFixed(2) + '"' +
          ' stroke-dashoffset="' + (-offset).toFixed(2) + '"></circle>'
      );
      offset += length;
    });

    return (
      '<svg class="donut" viewBox="0 0 ' + size + ' ' + size + '" role="img" aria-label="Score distribution">' +
        '<circle class="donut-track" cx="' + center + '" cy="' + center + '" r="' + radius + '"' +
          ' stroke-width="' + stroke + '" fill="none"></circle>' +
        '<g transform="rotate(-90 ' + center + ' ' + center + ')">' + segments.join('') + '</g>' +
        '<text class="donut-total" x="' + center + '" y="' + (center + 4) + '">' +
          esc(String(stats.practiced)) + '</text>' +
        '<text class="donut-caption" x="' + center + '" y="' + (center + 22) + '">practiced</text>' +
      '</svg>'
    );
  }

  function bucketWords(bucketId) {
    var stats = scoreStats();
    for (var i = 0; i < stats.buckets.length; i += 1) {
      if (stats.buckets[i].id === bucketId) return stats.buckets[i].words;
    }
    return [];
  }

  function bucketCopyText(bucketId) {
    return bucketWords(bucketId)
      .map(function (word) { return word.term; })
      .join('\n');
  }

  function bucketLabel(bucketId) {
    for (var i = 0; i < SCORE_BUCKETS.length; i += 1) {
      if (SCORE_BUCKETS[i].id === bucketId) return SCORE_BUCKETS[i].label;
    }
    return bucketId;
  }

  function renderExport() {
    var stats = scoreStats();

    var bucketRows = stats.buckets
      .map(function (bucket) {
        var count = bucket.words.length;
        return (
          '<li class="bucket-row">' +
            '<span class="bucket-swatch donut-' + bucket.id + '"></span>' +
            '<span class="bucket-label">' + esc(bucket.label) + '</span>' +
            '<span class="bucket-count">' + esc(String(count)) + (count === 1 ? ' word' : ' words') + '</span>' +
            '<button class="btn btn-sm btn-ghost" data-act="copy-bucket" data-bucket="' + esc(bucket.id) + '"' +
              (count ? '' : ' disabled') + '>Copy</button>' +
          '</li>'
        );
      })
      .join('');

    return (
      '<div class="view-head"><h1>Export</h1></div>' +
      '<p class="subtle">Scores are from the most recent session you finished completely, kept in this ' +
        'browser only (localStorage). Quitting a session leaves them untouched. Copies are plain text, ' +
        'one word per line.</p>' +
      '<section class="panel">' +
        '<h2>Score statistics <span class="subtle">(' + esc(String(stats.total)) + ' words in this bank)</span></h2>' +
        '<div class="export-stats">' +
          '<div class="donut-wrap">' + donutMarkup(stats) + '</div>' +
          '<div class="export-legend">' +
            '<ul class="bucket-list">' + bucketRows + '</ul>' +
            '<p class="subtle">Unpracticed: ' + esc(String(stats.unpracticed)) + ' words — never part of a ' +
              'completed session, not counted in the chart.</p>' +
          '</div>' +
        '</div>' +
      '</section>' +
      discardedSectionMarkup() +
      '<div class="copy-panel" id="copy-panel" hidden><textarea id="copy-area" readonly></textarea></div>'
    );
  }

  /* ----------------------------------------------------------- word view */

  function chipsMarkup(values, kindLabel, linked) {
    var clickable = linked !== false;
    return (
      '<div class="chip-cloud">' +
      values
        .map(function (value) {
          var target = clickable ? wordByKey.get(normKey(value)) : null;
          if (target) {
            return '<button class="chip" data-act="open-word" data-word-id="' + esc(target.id) + '">' +
              esc(value) + (kindLabel ? '<span class="chip-kind">' + esc(kindLabel) + '</span>' : '') + '</button>';
          }
          return '<span class="chip is-muted">' + esc(value) + '</span>';
        })
        .join('') +
      '</div>'
    );
  }

  /* 中文释义默认隐藏，点一下这块区域才显示；状态只属于当前卡片展示。 */
  function chineseRevealMarkup(word, shown) {
    return (
      '<div class="section chinese-reveal' + (shown ? ' is-shown' : '') + '"' +
        ' data-act="toggle-chinese" role="button" tabindex="0" aria-expanded="' + (shown ? 'true' : 'false') + '">' +
        '<h3>Chinese meaning</h3>' +
        (shown
          ? '<ul class="defs">' +
              word.chinese.map(function (item) { return '<li>' + esc(item) + '</li>'; }).join('') +
            '</ul>'
          : '<div class="chinese-blank"><span>tap to show</span></div>') +
      '</div>'
    );
  }

  function exampleMarkup(example) {
    var source = [];
    if (example.source_type) source.push(example.source_type);
    if (example.author) source.push(example.author);
    return (
      '<div class="example">' +
        '<div class="ex-sentence">' + esc(example.sentence) + '</div>' +
        (example.translation ? '<div class="ex-translation">' + esc(example.translation) + '</div>' : '') +
        (source.length
          ? '<div class="ex-source">' + esc(source.join(' · ')) +
            (example.url ? ' · <a href="' + esc(example.url) + '" target="_blank" rel="noopener noreferrer">source</a>' : '') +
            '</div>'
          : '') +
      '</div>'
    );
  }

  function answerSectionsMarkup(word, options) {
    var opts = options || {};
    var blocks = [];

    if (word.chinese.length) {
      if (opts.chineseReveal) {
        blocks.push(chineseRevealMarkup(word, !!session.chineseShown));
      } else {
        blocks.push(
          '<div class="section"><h3>Chinese meaning</h3><div class="meaning"><ul class="defs">' +
            word.chinese.map(function (item) { return '<li>' + esc(item) + '</li>'; }).join('') +
          '</ul></div></div>'
        );
      }
    }

    if (word.definitions.length) {
      blocks.push(
        '<div class="section"><h3>English definition</h3><ul class="defs">' +
          word.definitions
            .map(function (item) {
              return '<li>' + (item.pos ? '<span class="def-pos">' + esc(item.pos) + '</span>' : '') + esc(item.definition) + '</li>';
            })
            .join('') +
        '</ul></div>'
      );
    }

    if (word.synonyms.length) {
      blocks.push(
        '<div class="section"><h3>Synonyms</h3>' +
          chipsMarkup(word.synonyms, '', opts.linked !== false) +
        '</div>'
      );
    }

    if (word.examples.length) {
      blocks.push('<div class="section"><h3>Example</h3>' + exampleMarkup(word.examples[0]) + '</div>');
    }

    return blocks.join('');
  }

  function renderWord(wordId) {
    var word = wordById.get(wordId);
    if (!word) {
      return (
        '<div class="card"><h1 class="term">Word not found</h1>' +
        '<p class="subtle">' + esc(wordId) + ' is not in the current bank.</p>' +
        '<p><a class="btn" href="#/browse">Back to Bank</a></p></div>'
      );
    }

    var blocks = [];
    blocks.push(
      '<h1 class="term">' + esc(word.term) + '</h1>' +
      '<div class="term-meta">' +
        word.pos.map(function (item) { return '<span class="pos-chip">' + esc(item) + '</span>'; }).join('') +
        (word.lemma && normKey(word.lemma) !== normKey(word.term) ? '<span class="subtle">lemma: ' + esc(word.lemma) + '</span>' : '') +
        '<span class="subtle">' + esc(String(degreeOf(word.id))) + ' links</span>' +
      '</div>'
    );

    blocks.push(answerSectionsMarkup(word));

    if (word.derivatives.length) {
      blocks.push(
        '<div class="section"><h3>Derivatives</h3><ul class="defs">' +
          word.derivatives
            .map(function (item) {
              var linked = wordByKey.get(normKey(item.term));
              var label = linked
                ? '<button class="chip" data-act="open-word" data-word-id="' + esc(linked.id) + '">' + esc(item.term) + '</button>'
                : '<span class="chip is-muted">' + esc(item.term) + '</span>';
              return '<li>' + label + (item.relation ? ' <span class="subtle">' + esc(item.relation) + '</span>' : '') + '</li>';
            })
            .join('') +
        '</ul></div>'
      );
    }

    if (word.confusables.length) {
      blocks.push(
        '<div class="section"><h3>Confusables</h3><ul class="defs">' +
          word.confusables
            .map(function (item) {
              var linked = wordByKey.get(normKey(item.term));
              var label = linked
                ? '<button class="chip" data-act="open-word" data-word-id="' + esc(linked.id) + '">' + esc(item.term) + '</button>'
                : '<span class="chip is-muted">' + esc(item.term) + '</span>';
              return '<li>' + label + (item.distinction ? ' <span class="subtle">' + esc(item.distinction) + '</span>' : '') + '</li>';
            })
            .join('') +
        '</ul></div>'
      );
    }

    if (word.notes.length) {
      blocks.push(
        '<div class="section"><h3>Personal note</h3><ul class="notes">' +
          word.notes.map(function (item) { return '<li>' + esc(item) + '</li>'; }).join('') +
        '</ul></div>'
      );
    }

    var neighbors = neighborsOf(word.id).slice(0, 12);
    var graph = '';
    if (neighbors.length) {
      graph =
        '<div class="section"><h3>Local neighborhood</h3>' +
        '<div class="local-graph" id="local-graph" data-word-id="' + esc(word.id) + '"></div>' +
        '</div>';
    }

    if (!word.chinese.length && !word.definitions.length && !word.examples.length && !word.synonyms.length) {
      blocks.push('<p class="empty">This entry has no definition content yet.</p>');
    }

    if (word.flags.length) {
      blocks.push('<p class="flags" title="review flags from tools/validate.py">⚑ ' + esc(word.flags.join(' · ')) + '</p>');
    }

    return (
      '<div class="view-head">' +
        '<a class="btn btn-sm btn-ghost" href="#/browse">← Bank</a>' +
        '<span class="spacer"></span>' +
        discardToggleMarkup(word.id) +
        '<button class="btn btn-sm" data-act="start-default">Practice current list</button>' +
      '</div>' +
      '<article class="card">' + blocks.join('') + graph + '</article>'
    );
  }

  function localGraphMarkup(wordId, size) {
    var word = wordById.get(wordId);
    if (!word) return '';
    var neighbors = neighborsOf(word.id).slice(0, 12);
    if (!neighbors.length) return '';

    var W = size.width;
    var H = size.height;
    var cx = W / 2;
    var cy = H / 2;
    var radiusX = Math.max(70, W * 0.34);
    var radiusY = Math.max(48, H * 0.32);
    var positions = new Map();
    positions.set(word.id, { x: cx, y: cy });
    neighbors.forEach(function (item, index) {
      var angle = (index / neighbors.length) * Math.PI * 2 - Math.PI / 2;
      positions.set(item.id, {
        x: cx + Math.cos(angle) * radiusX,
        y: cy + Math.sin(angle) * radiusY,
        labDir: { x: Math.cos(angle), y: Math.sin(angle) }
      });
    });

    var radiusOf = new Map();
    radiusOf.set(word.id, 11);
    neighbors.forEach(function (item) { radiusOf.set(item.id, 6.5); });

    var links = [];
    neighbors.forEach(function (item) {
      item.kinds.forEach(function (kind) { links.push({ a: word.id, b: item.id, kind: kind }); });
    });

    return graphSvgMarkup(
      {
        width: W,
        height: H,
        positions: positions,
        radiusOf: radiusOf,
        links: links,
        localDegree: function (id) { return id === word.id ? neighbors.length : 0; }
      },
      { labelAll: true, centerId: word.id }
    );
  }

  /* --------------------------------------------------------- session view */

  function beginSession(wordIds, listId, listName) {
    var ids = [];
    var seen = new Set();
    wordIds.forEach(function (id) {
      if (wordById.has(id) && !seen.has(id)) {
        seen.add(id);
        ids.push(id);
      }
    });
    if (!ids.length) {
      showToast('This list has no words to practice');
      return;
    }
    session = createSession();
    session.active = true;
    session.listId = listId;
    session.listName = listName;
    session.order = ids;
    session.queue = ids.slice();
    ids.forEach(function (id) {
      session.status[id] = 'unseen';
      session.score[id] = 0;
    });
    go('#/session');
  }

  function startSession(listId) {
    var list = findList(listId) || findList('all');
    if (!list) return;
    ui.selectedListId = list.id;
    beginSession(list.word_ids, list.id, list.name);
  }

  function toggleCardFace() {
    if (!session.active) return;
    session.cardFace = session.cardFace === 'back' ? 'front' : 'back';
    // 翻回正面等于重新开始看这个词：中文恢复默认隐藏
    if (session.cardFace === 'front') session.chineseShown = false;
    render();
  }

  function toggleChinese() {
    if (!session.active) return;
    session.chineseShown = !session.chineseShown;
    render();
  }

  var SCORE_DELTA = { unknown: 1, vague: 2, known: 0 };

  /* 评分只改变当前词的掌握状态与本次 session 的 score，不会跳到下一个词。
     同一个词在同一轮展示期间反复修改评分不会重复累计：这一轮只记它被选过的最高档。 */
  function rate(kind) {
    if (!session.active || !(kind in SCORE_DELTA)) return;
    var word = currentWord();
    if (!word) return;
    session.status[word.id] = kind;
    if (SCORE_DELTA[kind] > (session.roundWorst[word.id] || 0)) {
      session.roundWorst[word.id] = SCORE_DELTA[kind];
    }
    render();
  }

  function commitRound() {
    Object.keys(session.roundWorst).forEach(function (id) {
      var add = session.roundWorst[id] || 0;
      if (add > 0) session.score[id] = Math.min(10, (session.score[id] || 0) + add);
    });
    session.roundWorst = Object.create(null);
  }

  function goNext() {
    if (!session.active) return;
    if (session.pos + 1 < session.queue.length) {
      session.pos += 1;
      session.cardFace = 'front';
      session.chineseShown = false;
      render();
      return;
    }
    endRound();
  }

  function goPrevious() {
    if (!session.active || session.pos === 0) return;
    session.pos -= 1;
    session.cardFace = 'front';
    session.chineseShown = false;
    render();
  }

  /* 一轮结束：结算本轮 score，再按“status !== known”组出下一轮；
     所有词都是 known 时正常结束 session。 */
  function endRound() {
    commitRound();
    session.roundsCompleted = session.round;
    var remaining = session.order.filter(function (id) {
      return session.status[id] !== 'known';
    });
    if (!remaining.length) {
      finishSession();
      go('#/result');
      return;
    }
    session.round += 1;
    session.queue = remaining;
    session.pos = 0;
    session.cardFace = 'front';
    session.chineseShown = false;
    render();
  }

  function buildResult() {
    var entries = [];
    var scores = {};
    var perfect = 0;
    session.order.forEach(function (id) {
      var word = wordById.get(id);
      if (!word) return;
      var score = Math.min(10, session.score[id] || 0);
      scores[id] = score;
      if (score > 0) entries.push({ id: id, term: word.term, score: score });
      else perfect += 1;
    });
    entries.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      return byTerm(a.term, b.term);
    });
    return {
      listId: session.listId,
      listName: session.listName,
      words: session.order.length,
      rounds: session.round,
      perfect: perfect,
      entries: entries,
      scores: scores
    };
  }

  function finishSession() {
    session.result = buildResult();
    session.active = false;
    /* 只有正常完成（所有词 known）才写入 localStorage；中途退出不写入也不覆盖。 */
    persistSessionScores(session.result);
  }

  function exitSession(skipConfirm) {
    if (!session.active) {
      go('#/');
      return;
    }
    if (!skipConfirm && !window.confirm('Quit this session? Progress is not saved.')) return;
    session = createSession();
    go('#/lists');
  }

  function knownCount() {
    var count = 0;
    session.order.forEach(function (id) {
      if (session.status[id] === 'known') count += 1;
    });
    return count;
  }

  function rateButtonMarkup(kind, label, key, current) {
    var selected = current === kind;
    return (
      '<button class="rate-btn rate-' + kind + (selected ? ' is-selected' : '') + '"' +
        ' data-act="rate" data-kind="' + kind + '"' +
        ' aria-pressed="' + (selected ? 'true' : 'false') + '">' +
        label + '<span class="key">' + key + '</span>' +
      '</button>'
    );
  }

  function renderSession() {
    var word = currentWord();
    if (!word) {
      return '<p class="empty">This round is over.</p><p><a class="btn" href="#/">Back to Garden</a></p>';
    }
    var total = session.order.length;
    var roundLeft = session.queue.filter(function (id) {
      return session.status[id] !== 'known';
    }).length;
    var progress = session.queue.length ? Math.min(100, (session.pos / session.queue.length) * 100) : 0;
    var status = session.status[word.id] || 'unseen';
    var onBack = session.cardFace === 'back';

    return (
      '<div class="session-bar">' +
        '<button class="btn btn-sm btn-ghost" data-act="exit-session">← Quit<span class="hide-narrow"> session</span></button>' +
        '<button class="btn btn-sm nav-btn" data-act="previous" aria-label="Previous word"' +
          (session.pos === 0 ? ' disabled' : '') + '>←</button>' +
        '<button class="btn btn-sm nav-btn" data-act="next" aria-label="Next word">→</button>' +
        '<span class="nav-pos">' + esc(String(session.pos + 1)) + ' / ' + esc(String(session.queue.length)) + '</span>' +
        discardToggleMarkup(word.id) +
      '</div>' +
      '<div class="session-progress">' +
        '<div class="progress"><span style="width:' + progress.toFixed(1) + '%"></span></div>' +
        '<span class="session-meta">Round ' + esc(String(session.round)) +
          ' · ' + esc(String(roundLeft)) + ' left · Known ' + esc(String(knownCount())) +
          ' / ' + esc(String(total)) + '</span>' +
      '</div>' +
      '<div class="flashcard" data-act="flip-card">' +
        '<p class="fc-term">' + esc(word.term) + '</p>' +
        (word.pos.length ? '<p class="fc-pos">' + esc(word.pos.join(' / ')) + '</p>' : '') +
        (onBack
          ? '<div class="fc-answer">' +
              (answerSectionsMarkup(word, { linked: false, chineseReveal: true }) ||
                '<p class="empty">This entry has nothing more to show.</p>') +
            '</div>'
          : '') +
        '<span class="fc-flip-hint">' + (onBack ? 'tap to flip back' : 'tap to flip') + '</span>' +
      '</div>' +
      '<div class="fc-actions">' +
        '<div class="rate-row">' +
          rateButtonMarkup('unknown', 'Unknown', '1', status) +
          rateButtonMarkup('vague', 'Fuzzy', '2', status) +
          rateButtonMarkup('known', 'Known', '3', status) +
        '</div>' +
      '</div>' +
      '<p class="kbd-hints">tap / Space = flip · 1/2/3 = unknown / fuzzy / known · ← → = previous / next · D = discard · Esc = quit</p>'
    );
  }

  /* ---------------------------------------------------------- result view */

  function resultCopyText(mode) {
    var entries = session.result ? session.result.entries : [];
    return entries
      .map(function (entry) {
        return mode === 'scores' ? entry.score + '\t' + entry.term : entry.term;
      })
      .join('\n');
  }

  function renderResult() {
    var result = session.result;
    if (!result) return '';
    var rows = result.entries
      .map(function (entry) {
        return '<li><span class="s-score">' + esc(String(entry.score)) + '</span>' +
          '<button class="s-term" data-act="open-word" data-word-id="' + esc(entry.id) + '">' + esc(entry.term) + '</button></li>';
      })
      .join('');

    return (
      '<div class="view-head"><h1>Session Complete</h1></div>' +
      '<section class="card">' +
        '<div class="result-grid">' +
          '<div class="stat"><b>' + esc(String(result.words)) + '</b><span class="subtle">Words</span></div>' +
          '<div class="stat"><b>' + esc(String(result.rounds)) + '</b><span class="subtle">Rounds</span></div>' +
          '<div class="stat"><b>' + esc(String(result.perfect)) + '</b><span class="subtle">First try</span></div>' +
        '</div>' +
        '<p class="subtle">List: ' + esc(result.listName) + '</p>' +
        '<div class="section"><h3>Words to revisit (score &gt; 0)</h3>' +
          (rows
            ? '<ul class="score-list">' + rows + '</ul>'
            : '<p class="empty">Nothing to revisit this time.</p>') +
        '</div>' +
        (result.entries.length
          ? '<div class="btn-row result-actions">' +
              '<button class="btn" data-act="copy" data-mode="words">Copy hard words</button>' +
              '<button class="btn" data-act="copy" data-mode="scores">Copy words + scores</button>' +
              '<button class="btn btn-ghost" data-act="restart">Restart</button>' +
              '<a class="btn btn-ghost" href="#/">Garden →</a>' +
            '</div>'
          : '<div class="btn-row result-actions">' +
              '<button class="btn btn-ghost" data-act="restart">Restart</button>' +
              '<a class="btn btn-ghost" href="#/">Garden →</a>' +
            '</div>') +
        '<div class="copy-panel" id="copy-panel" hidden><textarea id="copy-area" readonly></textarea></div>' +
      '</section>'
    );
  }

  function copyToClipboard(text) {
    if (!text) return Promise.resolve(false);
    var fallback = function () {
      try {
        var area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', 'readonly');
        area.style.position = 'fixed';
        area.style.top = '-1000px';
        document.body.appendChild(area);
        area.select();
        area.setSelectionRange(0, area.value.length);
        var ok = document.execCommand ? document.execCommand('copy') : false;
        document.body.removeChild(area);
        return !!ok;
      } catch (error) {
        return false;
      }
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
        return navigator.clipboard.writeText(text).then(
          function () { return true; },
          function () { return fallback(); }
        );
      }
    } catch (error) {
      /* 落到 fallback */
    }
    return Promise.resolve(fallback());
  }

  /* ---------------------------------------------------------- interactions */

  function copyWithPanel(text, label) {
    if (!text) {
      showToast('Nothing to copy');
      return;
    }
    copyToClipboard(text).then(function (ok) {
      var panel = document.getElementById('copy-panel');
      var area = document.getElementById('copy-area');
      if (ok) {
        if (panel) panel.hidden = true;
        showToast('Copied ' + label);
        return;
      }
      if (panel && area) {
        area.value = text;
        panel.hidden = false;
        area.focus();
        area.select();
      }
      showToast('Browser blocked automatic copying — copy manually');
    });
  }

  function discardedCopyText() {
    return discardedWords()
      .map(function (word) { return word.term; })
      .join('\n');
  }

  function onClick(event) {
    var nodeEl = event.target.closest ? event.target.closest('[data-word-id]') : null;
    var actionEl = event.target.closest ? event.target.closest('[data-act]') : null;

    if (actionEl) {
      var act = actionEl.getAttribute('data-act');
      if (act === 'open-word') {
        event.preventDefault();
        openWord(actionEl.getAttribute('data-word-id'));
        return;
      }
      if (act === 'start-default') {
        startSession(ui.selectedListId);
        return;
      }
      if (act === 'start-selected') {
        startSession(ui.selectedListId);
        return;
      }
      if (act === 'select-list') {
        ui.selectedListId = actionEl.getAttribute('data-list-id') || 'all';
        render();
        return;
      }
      if (act === 'resample') {
        ui.gardenSeed += 1;
        render();
        return;
      }
      if (act === 'load-more') {
        ui.browseLimit += 150;
        refreshBrowse();
        return;
      }
      if (act === 'flip-card') {
        toggleCardFace();
        return;
      }
      if (act === 'toggle-chinese') {
        toggleChinese();
        return;
      }
      if (act === 'previous') {
        goPrevious();
        return;
      }
      if (act === 'next') {
        goNext();
        return;
      }
      if (act === 'rate') {
        rate(actionEl.getAttribute('data-kind'));
        return;
      }
      if (act === 'exit-session') {
        exitSession(false);
        return;
      }
      if (act === 'copy') {
        var copyMode = actionEl.getAttribute('data-mode');
        copyWithPanel(
          resultCopyText(copyMode),
          copyMode === 'scores' ? 'words + scores' : 'hard words'
        );
        return;
      }
      if (act === 'toggle-discard') {
        toggleDiscard(actionEl.getAttribute('data-word-id'));
        return;
      }
      if (act === 'copy-discarded') {
        copyWithPanel(discardedCopyText(), 'discarded words');
        return;
      }
      if (act === 'copy-bucket') {
        var bucketId = actionEl.getAttribute('data-bucket');
        copyWithPanel(bucketCopyText(bucketId), 'score ' + bucketLabel(bucketId));
        return;
      }
      if (act === 'clear-discarded') {
        clearDiscarded(false);
        return;
      }
      if (act === 'restart') {
        var listId = session.result ? session.result.listId : 'all';
        startSession(listId);
        return;
      }
    }

    if (nodeEl) {
      openWord(nodeEl.getAttribute('data-word-id'));
    }
  }

  function onKeydown(event) {
    var target = event.target;
    var tag = target && target.tagName ? target.tagName.toLowerCase() : '';
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

    if (currentRoute().name === 'session' && session.active) {
      var onChineseToggle = target && target.getAttribute &&
        target.getAttribute('data-act') === 'toggle-chinese';
      if (onChineseToggle && (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar')) {
        event.preventDefault();
        toggleChinese();
        return;
      }
      if (event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        toggleCardFace();
        return;
      }
      if (event.key === 'ArrowLeft') { event.preventDefault(); goPrevious(); return; }
      if (event.key === 'ArrowRight') { event.preventDefault(); goNext(); return; }
      if (event.key === '1') { rate('unknown'); return; }
      if (event.key === '2') { rate('vague'); return; }
      if (event.key === '3') { rate('known'); return; }
      if (event.key === 'd' || event.key === 'D') {
        var card = currentWord();
        if (card) toggleDiscard(card.id);
        return;
      }
      if (event.key === 'Escape') { exitSession(false); return; }
      return;
    }

    if ((event.key === 'Enter' || event.key === ' ') && target && target.getAttribute && target.getAttribute('data-word-id')) {
      event.preventDefault();
      openWord(target.getAttribute('data-word-id'));
    }
  }

  function onInput(event) {
    var target = event.target;
    if (!target || !target.id) return;
    if (target.id === 'browse-search') {
      ui.query = target.value;
      ui.browseLimit = 150;
      refreshBrowse();
      return;
    }
    if (target.id === 'garden-search') {
      ui.gardenQuery = target.value;
      refreshGarden();
    }
  }

  function onChange(event) {
    var target = event.target;
    if (target && target.id === 'browse-list') {
      ui.browseListId = target.value;
      ui.browseLimit = 150;
      refreshBrowse();
    }
  }

  /* ---------------------------------------------------------------- boot */

  function boot() {
    topbarEl = document.getElementById('topbar');
    viewEl = document.getElementById('view');
    toastEl = document.getElementById('toast');

    document.addEventListener('click', onClick);
    document.addEventListener('input', onInput);
    document.addEventListener('change', onChange);
    document.addEventListener('keydown', onKeydown);
    window.addEventListener('hashchange', render);

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      if (resizeTimer) window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(fillDeferredGraphs, 150);
    });

    if (!location.hash) location.hash = '#/';
    else render();
  }

  /* 调试 / 测试用只读入口（不参与页面逻辑，也不持久化任何东西） */
  window.GRE_DEBUG = {
    wordCount: function () { return WORDS.length; },
    listSummary: function () {
      return LISTS.map(function (list) { return { id: list.id, name: list.name, words: list.word_ids.length }; });
    },
    edgeCount: function () { return EDGES.length; },
    degrees: function (id) { return degreeOf(id); },
    state: function () {
      return {
        active: session.active,
        listId: session.listId,
        round: session.round,
        roundsCompleted: session.roundsCompleted,
        pos: session.pos,
        queueLength: session.queue.length,
        total: session.order.length,
        cardFace: session.cardFace,
        chineseShown: session.chineseShown,
        status: Object.assign({}, session.status),
        score: Object.assign({}, session.score),
        roundWorst: Object.assign({}, session.roundWorst),
        result: session.result,
        perfect: session.result ? session.result.perfect : null,
        ui: Object.assign({}, ui)
      };
    },
    current: function () {
      var word = currentWord();
      return word ? { id: word.id, term: word.term, cardFace: session.cardFace } : null;
    },
    startIds: function (ids, label) {
      beginSession(ids, 'debug', label || 'Debug');
    },
    startList: function (listId) { startSession(listId); },
    flip: toggleCardFace,
    showChinese: toggleChinese,
    next: goNext,
    previous: goPrevious,
    rate: rate,
    exit: function () { exitSession(true); },
    discardKey: DISCARD_KEY,
    discarded: function () { return discardedIdsArray(); },
    toggleDiscard: toggleDiscard,
    clearDiscarded: function () { clearDiscarded(true); },
    scoreKey: SCORE_KEY,
    scores: function () {
      var out = {};
      Object.keys(storedScores).forEach(function (id) { out[id] = storedScores[id]; });
      return out;
    },
    stats: function () {
      var stats = scoreStats();
      return {
        practiced: stats.practiced,
        unpracticed: stats.unpracticed,
        total: stats.total,
        buckets: stats.buckets.map(function (bucket) {
          return { id: bucket.id, label: bucket.label, count: bucket.words.length };
        })
      };
    },
    bucketTerms: function (bucketId) {
      return bucketWords(bucketId).map(function (word) { return word.term; });
    },
    route: function () { return currentRoute(); }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
