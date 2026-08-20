//! 星枢引擎：现网 SQLite 原库，不改表。

pub mod embed;
pub mod engine;
pub mod extract;
pub mod http;
pub mod image;
pub mod llm;
pub mod rank;
pub mod rerank;
pub mod secrets;
pub mod site;
pub mod temporal;
pub mod types;

pub use engine::Engine;
pub use types::{PackOpts, SearchOpts, Hit, EMBED_DIM, PRODUCT, RELEASE, VERSION};
