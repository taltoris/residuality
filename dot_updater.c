/*
 * dot_updater.c
 * Residuality — surgical dot file updater
 *
 * Usage: tree-sitter query extract.scm <filepath> --json | dot_updater <graph.dot> <filepath>
 *
 * Reads tree-sitter JSON from stdin.
 * Surgically replaces the section for <filepath> in <graph.dot>.
 * All other sections are untouched.
 * Write is atomic via rename().
 *
 * Build: gcc -O2 -o dot_updater dot_updater.c cJSON.c -lm
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"

#define MAX_BUF      (8 * 1024 * 1024)  /* 8MB */
#define MAX_LINE     4096
#define MAX_NODES    4096
#define MAX_EDGES    8192
#define MAX_PATH     1024
#define MAX_SIG      512

/* ── Node and Edge structs ─────────────────────────────────────────────── */

typedef struct {
    char id[MAX_PATH];        /* "src/auth.py::AuthManager::verify_token" */
    char type[32];            /* "file", "class", "function", "section"   */
    char file[MAX_PATH];
    int  line_start;
    int  line_end;
    char label[MAX_SIG];      /* prose sections: label text               */
    char signature[MAX_SIG];  /* code: function/class signature           */
    char parent_id[MAX_PATH]; /* for contains edges                       */
} Node;

typedef struct {
    char from[MAX_PATH];
    char to[MAX_PATH];
    char label[64];           /* "contains", "imports", "calls", "precedes" */
} Edge;

/* ── File I/O helpers ──────────────────────────────────────────────────── */

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    char *buf = (char*)calloc(1, MAX_BUF);
    if (!buf) { fprintf(stderr, "OOM\n"); exit(1); }
    if (f) {
        fread(buf, 1, MAX_BUF - 1, f);
        fclose(f);
    }
    return buf;
}

static char *read_stdin(void) {
    char *buf = (char*)calloc(1, MAX_BUF);
    if (!buf) { fprintf(stderr, "OOM\n"); exit(1); }
    size_t total = 0, n;
    while ((n = fread(buf + total, 1, MAX_BUF - total - 1, stdin)) > 0)
        total += n;
    return buf;
}

static void write_file_atomic(const char *path, const char *content) {
    char tmp[MAX_PATH];
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    if (!f) { fprintf(stderr, "Cannot write %s\n", tmp); exit(1); }
    fputs(content, f);
    fclose(f);
    rename(tmp, path);
}

/* ── String helpers ────────────────────────────────────────────────────── */

static void escape_dot(const char *in, char *out, int outlen) {
    int j = 0;
    for (int i = 0; in[i] && j < outlen - 2; i++) {
        if (in[i] == '"') { out[j++] = '\\'; out[j++] = '"'; }
        else out[j++] = in[i];
    }
    out[j] = 0;
}

static int count_lines(const char *text, size_t len) {
    int n = 1;
    for (size_t i = 0; i < len; i++)
        if (text[i] == '\n') n++;
    return n;
}

/* ── Parse tree-sitter JSON output ────────────────────────────────────── */

/*
 * tree-sitter --json output is an array of matches.
 * Each match has "pattern", "captures" array.
 * Each capture has "name", "node" with "startPosition"/"endPosition" {row, column}.
 * Rows are 0-indexed; we convert to 1-indexed line numbers.
 */

static void parse_ts_json(const char *json_str, const char *filepath,
                           Node *nodes, int *node_count,
                           Edge *edges, int *edge_count) {
    cJSON *root = cJSON_Parse(json_str);
    if (!root) {
        fprintf(stderr, "dot_updater: failed to parse tree-sitter JSON\n");
        return;
    }

    /* Build file node first */
    Node *file_node = &nodes[(*node_count)++];
    snprintf(file_node->id,   sizeof(file_node->id),   "%s", filepath);
    snprintf(file_node->type, sizeof(file_node->type), "file");
    snprintf(file_node->file, sizeof(file_node->file), "%s", filepath);
    file_node->line_start = 1;
    file_node->line_end   = 0; /* filled below */
    file_node->parent_id[0] = 0;

    /* Track current class context for method parent resolution */
    char current_class_id[MAX_PATH] = {0};
    int  current_class_end = 0;

    int n_matches = cJSON_GetArraySize(root);
    for (int i = 0; i < n_matches && *node_count < MAX_NODES; i++) {
        cJSON *match    = cJSON_GetArrayItem(root, i);
        cJSON *captures = cJSON_GetObjectItem(match, "captures");
        if (!captures) continue;

        int n_caps = cJSON_GetArraySize(captures);

        /* Find the def capture and name capture in this match */
        char cap_name[64]  = {0};
        char cap_type[32]  = {0};
        int  start_row     = -1;
        int  end_row       = -1;
        char node_name[MAX_SIG] = {0};

        for (int c = 0; c < n_caps; c++) {
            cJSON *cap      = cJSON_GetArrayItem(captures, c);
            cJSON *cap_nm   = cJSON_GetObjectItem(cap, "name");
            cJSON *cap_node = cJSON_GetObjectItem(cap, "node");
            if (!cap_nm || !cap_node) continue;

            const char *cname = cap_nm->valuestring;

            if (strcmp(cname, "class.def") == 0 ||
                strcmp(cname, "function.def") == 0) {
                cJSON *sp = cJSON_GetObjectItem(cap_node, "startPosition");
                cJSON *ep = cJSON_GetObjectItem(cap_node, "endPosition");
                if (sp && ep) {
                    cJSON *sr = cJSON_GetObjectItem(sp, "row");
                    cJSON *er = cJSON_GetObjectItem(ep, "row");
                    if (sr) start_row = sr->valueint + 1;
                    if (er) end_row   = er->valueint + 1;
                }
                if (strcmp(cname, "class.def")    == 0) snprintf(cap_type, sizeof(cap_type), "class");
                if (strcmp(cname, "function.def") == 0) snprintf(cap_type, sizeof(cap_type), "function");
            }

            if (strcmp(cname, "class.name")    == 0 ||
                strcmp(cname, "function.name") == 0) {
                cJSON *txt = cJSON_GetObjectItem(cap_node, "text");
                if (txt && cJSON_IsString(txt))
                    snprintf(node_name, sizeof(node_name), "%s", txt->valuestring);
            }

            if (strcmp(cname, "import.module") == 0) {
                cJSON *txt = cJSON_GetObjectItem(cap_node, "text");
                if (txt && cJSON_IsString(txt) && *edge_count < MAX_EDGES) {
                    Edge *e = &edges[(*edge_count)++];
                    snprintf(e->from,  sizeof(e->from),  "%s", filepath);
                    snprintf(e->to,    sizeof(e->to),    "%s", txt->valuestring);
                    snprintf(e->label, sizeof(e->label), "imports");
                }
            }
        }

        if (start_row < 0 || !node_name[0] || !cap_type[0]) continue;

        /* Determine parent */
        char parent[MAX_PATH];
        if (strcmp(cap_type, "class") == 0) {
            snprintf(parent, sizeof(parent), "%s", filepath);
            /* Update class tracking */
            snprintf(current_class_id,  sizeof(current_class_id),
                     "%s::%s", filepath, node_name);
            current_class_end = end_row;
        } else {
            /* function — is it inside the current class? */
            if (current_class_id[0] && start_row <= current_class_end)
                snprintf(parent, sizeof(parent), "%s", current_class_id);
            else
                snprintf(parent, sizeof(parent), "%s", filepath);
        }

        /* Build node */
        Node *nd = &nodes[(*node_count)++];
        snprintf(nd->id,        sizeof(nd->id),        "%s::%s", parent, node_name);
        snprintf(nd->type,      sizeof(nd->type),      "%s", cap_type);
        snprintf(nd->file,      sizeof(nd->file),      "%s", filepath);
        snprintf(nd->parent_id, sizeof(nd->parent_id), "%s", parent);
        nd->line_start = start_row;
        nd->line_end   = end_row;

        /* Signature */
        if (strcmp(cap_type, "class") == 0)
            snprintf(nd->signature, sizeof(nd->signature), "class %s", node_name);
        else
            snprintf(nd->signature, sizeof(nd->signature), "%s(...)", node_name);

        /* Contains edge */
        if (*edge_count < MAX_EDGES) {
            Edge *e = &edges[(*edge_count)++];
            snprintf(e->from,  sizeof(e->from),  "%s", parent);
            snprintf(e->to,    sizeof(e->to),    "%s", nd->id);
            snprintf(e->label, sizeof(e->label), "contains");
        }

        /* Update file node line_end */
        if (end_row > file_node->line_end)
            file_node->line_end = end_row;
    }

    cJSON_Delete(root);
}

/* ── Generate dot section for one file ────────────────────────────────── */

static void generate_section(const char *filepath,
                              Node *nodes, int node_count,
                              Edge *edges, int edge_count,
                              char *out, size_t outlen) {
    size_t pos = 0;
    char esc[MAX_PATH * 2];

#define APPEND(...) do { \
    int _n = snprintf(out + pos, outlen - pos, __VA_ARGS__); \
    if (_n > 0) pos += _n; \
} while(0)

    escape_dot(filepath, esc, sizeof(esc));
    APPEND("    // --- file: %s ---\n", filepath);

    /* Nodes */
    for (int i = 0; i < node_count; i++) {
        Node *nd = &nodes[i];
        char id_esc[MAX_PATH * 2], sig_esc[MAX_SIG * 2];
        escape_dot(nd->id,        id_esc,  sizeof(id_esc));
        escape_dot(nd->signature, sig_esc, sizeof(sig_esc));

        if (strcmp(nd->type, "file") == 0) {
            APPEND("    \"%s\" [type=\"file\", line_start=%d, line_end=%d];\n",
                   id_esc, nd->line_start, nd->line_end);
        } else if (strcmp(nd->type, "class") == 0) {
            APPEND("    \"%s\" [type=\"class\", file=\"%s\", "
                   "line_start=%d, line_end=%d, signature=\"%s\"];\n",
                   id_esc, nd->file, nd->line_start, nd->line_end, sig_esc);
        } else if (strcmp(nd->type, "function") == 0) {
            APPEND("    \"%s\" [type=\"function\", file=\"%s\", "
                   "line_start=%d, line_end=%d, signature=\"%s\"];\n",
                   id_esc, nd->file, nd->line_start, nd->line_end, sig_esc);
        } else if (strcmp(nd->type, "section") == 0) {
            char lbl_esc[MAX_SIG * 2];
            escape_dot(nd->label, lbl_esc, sizeof(lbl_esc));
            APPEND("    \"%s\" [type=\"section\", file=\"%s\", "
                   "line_start=%d, line_end=%d, label=\"%s\"];\n",
                   id_esc, nd->file, nd->line_start, nd->line_end, lbl_esc);
        }
    }

    APPEND("\n");

    /* Edges */
    for (int i = 0; i < edge_count; i++) {
        Edge *e = &edges[i];
        char from_esc[MAX_PATH * 2], to_esc[MAX_PATH * 2];
        escape_dot(e->from,  from_esc, sizeof(from_esc));
        escape_dot(e->to,    to_esc,   sizeof(to_esc));
        APPEND("    \"%s\" -> \"%s\" [label=\"%s\"];\n",
               from_esc, to_esc, e->label);
    }

    APPEND("    // --- end: %s ---\n", filepath);
#undef APPEND
}

/* ── Replace file section in existing dot content ──────────────────────── */

static void replace_section(const char *dot, const char *filepath,
                             const char *new_section,
                             char *out, size_t outlen) {
    char start_marker[MAX_PATH + 32];
    char end_marker[MAX_PATH + 32];
    snprintf(start_marker, sizeof(start_marker), "    // --- file: %s ---", filepath);
    snprintf(end_marker,   sizeof(end_marker),   "    // --- end: %s ---",  filepath);

    const char *p_start = strstr(dot, start_marker);
    const char *p_end   = p_start ? strstr(p_start, end_marker) : NULL;

    if (p_start && p_end) {
        /* Move past end marker line */
        p_end += strlen(end_marker);
        while (*p_end == '\n') p_end++;

        size_t before_len = p_start - dot;
        snprintf(out, outlen, "%.*s%s\n%s",
                 (int)before_len, dot,
                 new_section,
                 p_end);
    } else {
        /* Section not found — insert before closing brace */
        const char *closing = strrchr(dot, '}');
        if (closing) {
            size_t before_len = closing - dot;
            snprintf(out, outlen, "%.*s\n%s\n}\n",
                     (int)before_len, dot,
                     new_section);
        } else {
            /* Empty/new dot file */
            snprintf(out, outlen,
                     "digraph residuality {\n\n%s\n}\n",
                     new_section);
        }
    }
}

/* ── Main ──────────────────────────────────────────────────────────────── */

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "Usage: <ts-json on stdin> | dot_updater <graph.dot> <filepath>\n");
        return 1;
    }

    const char *dot_path = argv[1];
    const char *filepath = argv[2];

    char *dot_content = read_file(dot_path);
    char *ts_json     = read_stdin();

    Node nodes[MAX_NODES]; int node_count = 0;
    Edge edges[MAX_EDGES]; int edge_count = 0;

    parse_ts_json(ts_json, filepath, nodes, &node_count, edges, &edge_count);

    char *new_section = (char*)calloc(1, MAX_BUF);
    char *result      = (char*)calloc(1, MAX_BUF * 2);
    if (!new_section || !result) { fprintf(stderr, "OOM\n"); return 1; }

    generate_section(filepath, nodes, node_count, edges, edge_count,
                     new_section, MAX_BUF);

    replace_section(dot_content, filepath, new_section, result, MAX_BUF * 2);

    write_file_atomic(dot_path, result);

    free(dot_content);
    free(ts_json);
    free(new_section);
    free(result);
    return 0;
}
