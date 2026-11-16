import next from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const config = [
  { ignores: [".next/**", "node_modules/**", "src/lib/api-types.ts", "next-env.d.ts"] },
  ...next,
  ...nextTypescript,
  {
    rules: {
      // Text from a questionnaire, a source or a model is shown as text, never as markup.
      "react/no-danger": "error",
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Untrusted text must be rendered as text.",
        },
      ],
    },
  },
];
export default config;
