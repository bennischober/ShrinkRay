/**
 * @name Uncontrolled data used in path expression (boundary-aware)
 * @description Accessing paths influenced by users can allow an attacker to access unexpected resources. This variant teaches the analyzer this repository's own path-containment boundary (shrinkray.server.app.validate_path), so validated paths are not reported while unvalidated flows still are.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 7.5
 * @precision high
 * @id py/path-injection-validated
 * @tags correctness
 *       security
 *       external/cwe/cwe-022
 *       external/cwe/cwe-023
 *       external/cwe/cwe-036
 *       external/cwe/cwe-073
 *       external/cwe/cwe-099
 */

import python
import semmle.python.security.dataflow.PathInjectionQuery
import ValidatePathSanitizer
import PathInjectionFlow::PathGraph

from PathInjectionFlow::PathNode source, PathInjectionFlow::PathNode sink
where PathInjectionFlow::flowPath(source, sink)
select sink.getNode(), source, sink, "This path depends on a $@.", source.getNode(),
  "user-provided value"
