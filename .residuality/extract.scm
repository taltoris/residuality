; Residuality tree-sitter query for Python files
; Extracts classes, functions/methods, and imports with line positions

; Class definitions
(class_definition
  name: (identifier) @class.name) @class.def

; Function and method definitions
(function_definition
  name: (identifier) @function.name) @function.def

; from X import Y
(import_from_statement
  module_name: (dotted_name) @import.module
  name: (dotted_name) @import.name)

; import X
(import_statement
  name: (dotted_name) @import.module)
